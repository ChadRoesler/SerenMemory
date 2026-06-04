"""
seren_memory.consolidator.service
════════════════════════════════════════════════════════════════════════

The dream-cycle. Runs every ~20 hours (config). Does the memory worker's
job from Inside Out: walks short-term, decides what's worth keeping,
promotes it to long-term, ages out the rest, maintains near-term, honors
forget-flags.

THE CYCLE, IN ORDER:

  1. Read the latest daily brief (if any). It steers the search - the main
     model's "here's what mattered" note tells the consolidator what to
     look for.

  2. Honor forget-flags on long-term. PII → purge. Disputed → supersede
     with a corrected entry or just mark. (The flag carries the reason; we
     branch on keyword heuristics + optionally ask the model.)

  3. Promote short-term → long-term:
       - cluster short-term entries by topic + similarity
       - clusters with >= promote_min_evidence entries get consolidated
         into ONE long-term entry (model writes the synthesis)
       - brief promote_hints lower the threshold; noise_hints raise it
       - pinned entries always promote regardless of cluster size
       - verbatim entries peel off directly to long-term (no draft gate)
       - all other cluster syntheses land as DraftEntry (pending model review)

  4. Age out short-term: entries older than short_term_seconds that DIDN'T
     get promoted get archived to 'pruned' (safety net) then deleted.

  5. Maintain near-term:
       - expired (past expires_at) → drop
       - completed → promote to long-term as a record, then drop from near
       - long-unfulfilled → review (model decides keep/let-go)

  6. Sweep the pruned safety net (true-delete past the safety window).

MODEL USAGE: minimal. The model is used for (a) synthesizing a cluster of
short-term entries into one long-term statement, (b) redrafting when the
main model rejects with a critique, and (c) optionally judging ambiguous
forget-flags / stale intents. Everything else is mechanical (clustering via
embeddings, timestamp comparisons). A 2B-4B model is plenty.

THIS RUN IS IDEMPOTENT-ISH: re-running won't double-promote (promoted
entries are removed from short-term) but a crash mid-run could leave
short-term partially aged. That's acceptable - next run picks up where it
left off. We process in a defined order so partial completion degrades
gracefully.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

import httpx

from ..collections import MemoryStore
from ..config import MemoryConfig
from ..models.schemas import (
    ConsolidatorRun,
    ConsolidatorRunStatus,
    DailyBrief,
    DraftEntry,
    LongTermEntry,
    Source,
)

import threading

class ConsolidatorBusy(RuntimeError):
    """Raised when a force-run is requested while a scheduled run is mid-flight."""

class Consolidator:
    def __init__(self, store: MemoryStore, config: MemoryConfig, log=None):
        self._store = store
        self._cfg = config
        self._log = log or (lambda m: print(f"[consolidator] {m}"))
        self._run_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────
    #  Entry point - one full consolidation pass.
    # ──────────────────────────────────────────────────────────────────
    def run_once(self) -> dict[str, Any]:
        """Public entry point - serializes runs via _run_lock.

        Both the scheduled-loop and POST /consolidate/run call into here.
        Without the lock they could race (scheduled tick fires while a
        manual run is mid-flight, or vice versa) and corrupt the
        ConsolidatorRun record / counts. Non-blocking acquire means a
        collision raises ConsolidatorBusy rather than silently waiting -
        callers decide whether to retry, 409, or skip.
        """
        if not self._run_lock.acquire(blocking=False):
            raise ConsolidatorBusy(
                "a consolidator run is already in progress")
        try:
            return self._run_once_impl()
        finally:
            self._run_lock.release()

    def _run_once_impl(self) -> dict[str, Any]:
        """One full consolidation pass. Called only via run_once (which
        holds the lock).

        ALWAYS produces a ConsolidatorRun record - success, error, or noop -
        via the try/finally below. That's what gives 'last_consolidation_at'
        a durable, queryable answer instead of a runtime-only detail.

        Brief acquisition follows the push-wins-pull-is-fallback pattern:
        if the assistant left a fresh brief (created after our last
        successful run), use it; otherwise pull one ad-hoc from recent
        short-term via the consolidator's own model (Oliver-Twist).
        """
        start = time.time()
        self._log("consolidation window opening")
        report: dict[str, Any] = {
            "started_at": start,
            "promoted": 0,
            "drafted": 0,
            "aged_out": 0,
            "near_expired": 0,
            "near_completed_promoted": 0,
            "forget_flags_handled": 0,
            "pruned_swept": 0,
        }

        # Captured for the run record regardless of success/failure path.
        brief_id_used: Optional[str] = None
        brief_was_pulled = False
        status = ConsolidatorRunStatus.SUCCESS
        error_msg: Optional[str] = None

        try:
            # ── Brief acquisition: push wins, pull is the Oliver-Twist fallback ──
            brief = self._ensure_fresh_brief()
            if brief:
                brief_id_used = brief.get("id")
                brief_was_pulled = brief.get("_pulled", False)
                promote_hints = set(brief.get("metadata", {}).get("promote_hints", []) or [])
                noise_hints = set(brief.get("metadata", {}).get("noise_hints", []) or [])
                self._log(
                    f"using brief id={brief_id_used} pulled={brief_was_pulled}: "
                    f"{len(promote_hints)} promote-hints, {len(noise_hints)} noise-hints"
                )
            else:
                promote_hints = set()
                noise_hints = set()
                self._log("no brief available (push absent + pull failed); proceeding with mechanical heuristics only")

            # ── The existing work, unchanged ──
            report["forget_flags_handled"] = self._handle_forget_flags()
            promotion = self._promote_short_term(promote_hints, noise_hints, brief_id_used)
            report["promoted"] = promotion["promoted"]
            report["drafted"] = promotion["drafted"]
            report["aged_out"] = self._age_out_short_term()
            near = self._maintain_near_term()
            report["near_expired"] = near["expired"]
            report["near_completed_promoted"] = near["completed_promoted"]
            report["pruned_swept"] = self._store.sweep_pruned(
                self._cfg.consolidator.pruned_safety_days * 24 * 3600)

            # Noop detection: ran cleanly but touched nothing. Operationally
            # distinct from "did work" - useful for the viewer to show "system
            # is healthy and quiet" vs "system is doing things." Drafted
            # counts as work even though the long-term commit is pending -
            # the consolidator did its job by surfacing the candidate.
            total_work = (report["forget_flags_handled"] + report["promoted"]
                          + report["drafted"]
                          + report["aged_out"] + report["near_expired"]
                          + report["near_completed_promoted"] + report["pruned_swept"])
            if total_work == 0 and not brief_was_pulled:
                status = ConsolidatorRunStatus.NOOP

        except Exception as e:  # noqa: BLE001 - record any failure
            status = ConsolidatorRunStatus.ERROR
            error_msg = f"{type(e).__name__}: {e}"
            self._log(f"consolidation error: {error_msg}")

        finally:
            # ALWAYS persist a run record. This is the durable answer to
            # "when did the consolidator last run, and how did it go."
            finished = time.time()
            report["duration_seconds"] = round(finished - start, 2)
            try:
                report["counts_after"] = self._store.counts()
            except Exception:  # noqa: BLE001
                report["counts_after"] = {}

            try:
                run = ConsolidatorRun(
                    started_at=start,
                    finished_at=finished,
                    duration_seconds=report["duration_seconds"],
                    status=status,
                    promoted=report["promoted"],
                    drafted=report["drafted"],
                    aged_out=report["aged_out"],
                    near_expired=report["near_expired"],
                    near_completed_promoted=report["near_completed_promoted"],
                    forget_flags_handled=report["forget_flags_handled"],
                    pruned_swept=report["pruned_swept"],
                    brief_id_used=brief_id_used,
                    brief_was_pulled=brief_was_pulled,
                    error=error_msg,
                    counts_after=report["counts_after"],
                )
                self._store.add_run(run)
            except Exception as persist_err:  # noqa: BLE001
                # Run-record persistence is observability, not core function.
                # If it fails, log loudly but DON'T raise - the consolidator
                # returning a report is more important than self-observation.
                self._log(f"FAILED to persist consolidator run record: {persist_err}")

            self._log(f"window closed: status={status.value} {report}")

        # Preserve existing return shape, plus status & brief info for callers
        report["status"] = status.value
        report["brief_id_used"] = brief_id_used
        report["brief_was_pulled"] = brief_was_pulled
        if error_msg:
            report["error"] = error_msg
        return report

    # ──────────────────────────────────────────────────────────────────
    #  Step 1: brief acquisition (push wins, pull is fallback)
    # ──────────────────────────────────────────────────────────────────
    def _ensure_fresh_brief(self) -> Optional[dict[str, Any]]:
        """Get a brief to steer THIS run.

        A brief is FRESH if it was created after our last successful run -
        otherwise it was already consumed last cycle and we don't double-
        steer with stale guidance. If no fresh brief exists, the consolidator
        pulls one ad-hoc from recent short-term (Oliver-Twist).

        Returns the brief dict (chroma shape: {id, content, metadata}) with
        an extra '_pulled' bool marking the path, or None if no brief is
        available AND the pull failed.
        """
        brief = self._store.get_latest_brief()
        last_run = self._store.get_latest_run()

        # Use the existing brief if it post-dates our last run.
        if brief is not None:
            brief_created = brief.get("metadata", {}).get("created_at", 0)
            last_run_finished = (
                last_run.get("metadata", {}).get("finished_at", 0)
                if last_run else 0
            )
            if brief_created > last_run_finished:
                brief["_pulled"] = False
                return brief

        # No fresh brief - Oliver Twist time.
        return self._pull_brief()

    def _pull_brief(self) -> Optional[dict[str, Any]]:
        """Generate a brief from recent short-term via the consolidator's own
        model (the local Nemotron, NOT the main 9B - cross-LAN call gains
        nothing for retroactive reconstruction).

        Degrades gracefully: if the model is unreachable or returns non-JSON,
        returns None and the cycle continues with no steering (mechanical
        thresholds only).
        """
        rows = self._store.get_short_all(limit=self._cfg.consolidator.max_entries_per_run)
        if not rows:
            self._log("no short-term entries to pull a brief from")
            return None

        # Cap context fed to the model - on a Nano-floor cluster we can't
        # afford to dump 500 entries into a prompt. Recent 50, each capped
        # at 200 chars, with topic prefix to preserve clustering signal.
        sample = rows[-50:]
        fragments = []
        for r in sample:
            topic = r["metadata"].get("topic") or "untagged"
            content = (r["content"] or "")[:200]
            fragments.append(f"[{topic}] {content}")

        prompt = (
            "You are a memory consolidator's steering assistant. Below are "
            "recent short-term memory fragments. Produce a brief in this "
            "EXACT JSON shape (no markdown, no preamble, just JSON):\n"
            '{\n'
            '  "summary": "one short paragraph: what mattered in this period",\n'
            '  "promote_hints": ["topic phrases that should be remembered durably"],\n'
            '  "noise_hints": ["topic phrases that are noise and should not promote"],\n'
            '  "completed_intents": ["intents that look done based on the fragments"]\n'
            '}\n\n'
            "Fragments:\n" + "\n".join(fragments) + "\n\nJSON brief:"
        )

        try:
            raw = self._call_model(prompt, max_tokens=400)
        except Exception as e:  # noqa: BLE001
            self._log(f"brief pull model call failed ({e}); proceeding without a brief")
            return None

        # Lenient JSON parse - strip markdown fences if the model added them
        # despite the instruction (a 4B model will sometimes do this anyway).
        import json as _json
        txt = (raw or "").strip()
        if txt.startswith("```"):
            lines = txt.splitlines()
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            if lines and lines[0].strip().startswith("```"):
                lines = lines[1:]
            txt = "\n".join(lines)
        try:
            parsed = _json.loads(txt)
        except _json.JSONDecodeError as e:
            self._log(f"brief pull: model returned non-JSON ({e}); proceeding without")
            return None

        # Build a real DailyBrief and persist it. Subsequent runs then see
        # this brief in chroma like any other - consistency over special-casing.
        try:
            brief_obj = DailyBrief(
                summary=str(parsed.get("summary", "")),
                promote_hints=[str(h) for h in (parsed.get("promote_hints") or [])],
                noise_hints=[str(h) for h in (parsed.get("noise_hints") or [])],
                completed_intents=[str(i) for i in (parsed.get("completed_intents") or [])],
            )
            self._store.add_brief(brief_obj)
        except Exception as e:  # noqa: BLE001
            self._log(f"brief pull: built brief but failed to persist ({e}); using in-memory")
            return {
                "id": "(in-memory)",
                "content": str(parsed.get("summary", "")),
                "metadata": {
                    "created_at": time.time(),
                    "promote_hints": parsed.get("promote_hints") or [],
                    "noise_hints": parsed.get("noise_hints") or [],
                    "completed_intents": parsed.get("completed_intents") or [],
                },
                "_pulled": True,
            }

        self._log(
            f"pulled brief: {len(brief_obj.promote_hints)} promote-hints, "
            f"{len(brief_obj.noise_hints)} noise-hints"
        )
        return {
            "id": brief_obj.id,
            "content": brief_obj.summary,
            "metadata": {
                "created_at": brief_obj.created_at,
                "promote_hints": brief_obj.promote_hints,
                "noise_hints": brief_obj.noise_hints,
                "completed_intents": brief_obj.completed_intents,
            },
            "_pulled": True,
        }

    # ──────────────────────────────────────────────────────────────────
    #  Step 2: forget-flags
    # ──────────────────────────────────────────────────────────────────
    def _handle_forget_flags(self) -> int:
        rows = self._store.get_long_all()
        flagged = [r for r in rows if r["metadata"].get("forget_flag")]
        handled = 0
        for r in flagged:
            reason = str(r["metadata"]["forget_flag"]).lower()
            # PII / secret keywords → hard purge (true delete). This is the
            # ONE place SerenMemory truly deletes long-term content, and only
            # because leaving PII in is worse than the no-delete principle.
            if any(k in reason for k in ("ssn", "password", "secret", "pii",
                                          "credit card", "private key", "token")):
                self._store.long.delete(ids=[r["id"]])
                self._log(f"purged long-term {r['id']} (PII flag: {reason[:40]})")
                handled += 1
            else:
                # Non-PII flag: demote by zeroing evidence + marking. We keep
                # the content (history) but it'll rank near-bottom in recall.
                meta = dict(r["metadata"])
                meta["evidence_count"] = 0
                meta["demoted_reason"] = meta.pop("forget_flag")
                self._store.long.update(ids=[r["id"]], metadatas=[meta])
                self._log(f"demoted long-term {r['id']} (flag: {reason[:40]})")
                handled += 1
        return handled

    # ──────────────────────────────────────────────────────────────────
    #  Step 3: promote short-term → long-term
    # ──────────────────────────────────────────────────────────────────
    def _promote_short_term(self, promote_hints: set[str], noise_hints: set[str],
                             brief_id: Optional[str] = None) -> dict[str, int]:
        """Promote / draft eligible short-term clusters.

        Returns a counters dict: {"promoted": N, "drafted": M}.

        - "promoted" counts AUTO-COMMITS: verbatim peel-offs go straight to
          long-term because the verbatim flag is the human's explicit
          review-in-advance.
        - "drafted" counts cluster syntheses queued for HITL review. Those
          shorts STAY in the pool until the draft is approved (then
          archived to pruned) or rejected (then back in the cluster).

        brief_id is recorded on each draft for later "why did this come up"
        observability.
        """
        rows = self._store.get_short_all(limit=self._cfg.consolidator.max_entries_per_run)
        if not rows:
            return {"promoted": 0, "drafted": 0}

        # Cluster by topic first (cheap exact-match grouping), then within
        # each topic we trust they're similar enough. Entries with no topic
        # get clustered by similarity via the model-free approach: group
        # untagged entries into a single "untagged" bucket and let the model
        # decide if any cohere. (A fancier version would embed-cluster; v1
        # keeps it simple - topic tags do most of the work.)
        clusters: dict[str, list[dict]] = {}
        for r in rows:
            topic = r["metadata"].get("topic") or "_untagged"
            clusters.setdefault(topic, []).append(r)

        # Surface cluster shape for journalctl so noop runs aren't a mystery.
        cluster_shape = {t: len(es) for t, es in clusters.items()}
        self._log(
            f"clusters formed from {len(rows)} short entries: {cluster_shape}")

        promoted = 0
        drafted = 0
        promoted_ids: list[str] = []

        for topic, entries in clusters.items():
            # ── Peel off verbatim entries first ──
            # When Rhys flagged an entry's exact phrasing as the point (not
            # the gist), respect it. These go to long-term AS-IS, individually,
            # no synthesis. The rest of the cluster follows the normal path.
            verbatim_entries = [e for e in entries if e["metadata"].get("verbatim")]
            for ve in verbatim_entries:
                v_entry = LongTermEntry(
                    content=ve["content"],
                    topic=None if topic == "_untagged" else topic,
                    evidence_count=1,
                    source=Source.CONSOLIDATOR,
                    extra={"preserved_verbatim": True, "original_short_id": ve["id"]},
                )
                self._store.add_long(v_entry)
                promoted += 1
                promoted_ids.append(ve["id"])
                self._log(f"promoted verbatim entry {ve['id']} → long-term as-is")

            # ── Normal cluster path for non-verbatim remainder ──
            remaining = [e for e in entries if not e["metadata"].get("verbatim")]
            if not remaining:
                continue

            pinned = [e for e in remaining if e["metadata"].get("pinned")]
            threshold = self._cfg.consolidator.promote_min_evidence

            # Brief hints adjust the threshold. We check against BOTH the
            # cluster topic AND the concatenated entry contents - brief
            # hints describe content-semantics ("paisly pattern") while
            # cluster topics are taxonomy labels ("preference"). Topic-only
            # matching meant the brief correctly identified the entries but
            # couldn't route them to promotion. The haystack bridges that
            # gap; the "||" separator prevents accidental matches across
            # the topic→content boundary. We use ALL entries (including
            # verbatim ones) for hint matching because the brief was
            # generated against the full cluster's worth of content, so
            # the hints apply to the full cluster's worth.
            topic_l = topic.lower()
            content_haystack = " ".join(e["content"].lower() for e in entries)
            haystack = topic_l + " || " + content_haystack
            if any(h.lower() in haystack for h in promote_hints):
                threshold = 1
                self._log(
                    f"cluster '{topic}': promote hint matched → threshold=1")
            if any(h.lower() in haystack for h in noise_hints):
                threshold = 999
                self._log(
                    f"cluster '{topic}': noise hint matched → threshold=999")

            self._log(
                f"cluster '{topic}': entries={len(remaining)}, "
                f"pinned={len(pinned)}, threshold={threshold}")
            should_promote = pinned or len(remaining) >= threshold
            if not should_promote:
                self._log(
                    f"cluster '{topic}': skipped - "
                    f"{len(remaining)}<{threshold} and no pinned")
                continue

            synthesis = self._synthesize(topic, remaining)
            if not synthesis:
                continue

            # ── Model-review gate: cluster synthesis goes to drafts ──
            # Source shorts STAY in the pool. On approve, they archive to
            # pruned and the draft becomes durable. On reject with critique,
            # the reject endpoint triggers a redraft pass (up to
            # max_redraft_attempts). The verbatim peel-off path above
            # bypasses this gate - verbatim is the explicit review-in-advance.
            draft = DraftEntry(
                content=synthesis,
                topic=None if topic == "_untagged" else topic,
                evidence_count=len(remaining),
                source_short_ids=[e["id"] for e in remaining],
                brief_id_used=brief_id,
                source=Source.CONSOLIDATOR,
                attempt=1,
            )
            # cluster_id is the first draft's own id - the stable anchor
            # for the whole chain. Set it before persisting.
            draft.cluster_id = draft.id
            self._store.add_draft(draft)
            drafted += 1
            self._log(
                f"cluster '{topic}': drafted (id={draft.id}, {len(remaining)} entries) "
                f"→ awaiting model review")

        # Only verbatim auto-commit ids are in promoted_ids; cluster shorts
        # stay until their draft is approved/rejected/selected. The delete
        # here removes the verbatim sources whose essence now lives in
        # long-term as-is.
        if promoted_ids:
            self._store.delete_short(promoted_ids)

        return {"promoted": promoted, "drafted": drafted}

    @staticmethod
    def _strip_reasoning(text: str) -> str:
        """Nemotron-3-Nano emits <think>...</think> blocks by default because
        it's a reasoning model. Strip them so the long-term entry contains
        only the synthesized conclusion.

        Fallback behavior when </think> is missing: return text as-is.
        That covers two cases - model wasn't in reasoning mode that turn,
        or output was truncated mid-think. Returning text-as-is is safer
        than returning empty (which would trip the fallback to longest-content
        and silently lose the synthesis).
        """
        if "</think>" in text:
            return text.split("</think>")[-1]
        return text

    def _synthesize(self, topic: str, entries: list[dict]) -> Optional[str]:
        """Ask the model to fuse a cluster of short-term entries into one
        durable statement. This is the consolidator's main model use - a
        small summarization task well within a 2B model's range.

        Falls back to a mechanical join if the model is unreachable, so
        consolidation degrades rather than stalls when inference is down."""
        contents = [e["content"] for e in entries]

        # Mechanical fallback - just the longest entry, or a joined summary.
        # Used if the model call fails.
        fallback = max(contents, key=len) if contents else ""

        prompt = (
            "You are a memory consolidator. Below are several short-term "
            "memory fragments about the same topic. Fuse them into ONE "
            "concise, durable statement of fact - present tense, no "
            "preamble, no 'the user said'. Just the consolidated truth.\n\n"
            f"Topic: {topic}\n\nFragments:\n"
            + "\n".join(f"- {c}" for c in contents)
            + "\n\nConsolidated statement:"
        )

        try:
            result = self._call_model(prompt, max_tokens=200)
            if result:
                result = self._strip_reasoning(result)
            return result.strip() if result else fallback
        except Exception as exc:
            self._log(f"_synthesize raised {type(exc).__name__}: {exc}")
            return fallback

    # ──────────────────────────────────────────────────────────────────
    #  Redraft - triggered by the reject endpoint when the main model
    #  sends a critique back.
    # ──────────────────────────────────────────────────────────────────
    def redraft_cluster(self, cluster_id: str, rejected_draft_id: str,
                        critique: str, attempt: int,
                        source_short_ids: list[str],
                        brief_id_used: Optional[str],
                        topic: Optional[str],
                        evidence_count: int) -> Optional[dict[str, Any]]:
        """Public entry point for the reject endpoint. Synthesizes a new
        draft for the cluster, incorporating the critique and all previous
        attempts as context. If max_redraft_attempts is reached, flips the
        whole chain to requires_selection instead.

        Returns a dict: {action: 'redrafted'|'requires_selection',
                         draft_id: str|None, attempt: int}.
        """
        max_attempts = self._cfg.consolidator.max_redraft_attempts
        next_attempt = attempt + 1

        if next_attempt > max_attempts:
            # Exhausted - flip chain to requires_selection.
            self._store.mark_chain_requires_selection(cluster_id)
            self._log(
                f"cluster '{cluster_id}': {max_attempts} attempts exhausted "
                f"→ requires_selection")
            return {"action": "requires_selection", "draft_id": None,
                    "attempt": attempt}

        # Fetch the full chain so the new synthesis prompt can reference
        # all prior attempts.
        chain = self._store.get_drafts_by_cluster(cluster_id)
        previous_contents = [r["content"] for r in chain]
        previous_ids = [r["id"] for r in chain]

        # Fetch source shorts (some may be gone if manually deleted).
        if source_short_ids:
            existing = self._store.short.get(
                ids=source_short_ids, include=["documents", "metadatas"])
            from ..collections import _zip_get
            short_rows = _zip_get(existing, None)
        else:
            short_rows = []

        synthesis = self._redraft_synthesis(
            topic=topic or "_untagged",
            entries=short_rows,
            previous_drafts=previous_contents,
            critique=critique,
        )
        if not synthesis:
            self._log(
                f"cluster '{cluster_id}': redraft synthesis failed - "
                f"no new draft created")
            return None

        new_draft = DraftEntry(
            content=synthesis,
            topic=topic,
            evidence_count=evidence_count,
            source_short_ids=source_short_ids,
            brief_id_used=brief_id_used,
            cluster_id=cluster_id,
            attempt=next_attempt,
            previous_draft_ids=previous_ids,
            source=Source.CONSOLIDATOR,
        )
        self._store.add_draft(new_draft)
        self._log(
            f"cluster '{cluster_id}': redrafted (attempt {next_attempt}, "
            f"id={new_draft.id})")
        return {"action": "redrafted", "draft_id": new_draft.id,
                "attempt": next_attempt}

    def _redraft_synthesis(self, topic: str, entries: list[dict],
                           previous_drafts: list[str],
                           critique: str) -> Optional[str]:
        """Re-synthesize a cluster incorporating the model's critique and
        all previous attempts as negative context."""
        contents = [e["content"] for e in entries]
        fallback = max(contents, key=len) if contents else ""

        prev_block = "\n".join(
            f"Attempt {i+1}: {d}" for i, d in enumerate(previous_drafts)
        )
        prompt = (
            "You are a memory consolidator. A previous synthesis was rejected "
            "by the main model. Your task is to produce an improved version.\n\n"
            f"Topic: {topic}\n\n"
            "Source fragments:\n"
            + "\n".join(f"- {c}" for c in contents)
            + f"\n\nPrevious attempt(s):\n{prev_block}\n\n"
            f"Critique of previous attempt(s): {critique}\n\n"
            "Produce ONE concise, durable statement of fact that addresses "
            "the critique. Present tense, no preamble, no 'the user said'.\n\n"
            "Improved consolidated statement:"
        )

        try:
            result = self._call_model(prompt, max_tokens=200)
            if result:
                result = self._strip_reasoning(result)
            return result.strip() if result else fallback
        except Exception as exc:
            self._log(f"_redraft_synthesis raised {type(exc).__name__}: {exc}")
            return fallback

    # ──────────────────────────────────────────────────────────────────
    #  Step 4: age out short-term
    # ──────────────────────────────────────────────────────────────────
    def _age_out_short_term(self) -> int:
        rows = self._store.get_short_all(limit=None)
        now = time.time()
        cutoff = self._cfg.lifetimes.short_term_seconds
        stale = [r for r in rows
                 if not r["metadata"].get("pinned")
                 and (now - r["metadata"].get("ts", now)) > cutoff]
        if not stale:
            return 0
        # Safety net first, then delete.
        if self._cfg.consolidator.pruned_safety_days > 0:
            self._store.archive_pruned(stale)
        self._store.delete_short([r["id"] for r in stale])
        self._log(f"aged out {len(stale)} short-term entries (archived to pruned)")
        return len(stale)

    # ──────────────────────────────────────────────────────────────────
    #  Step 5: maintain near-term
    # ──────────────────────────────────────────────────────────────────
    def _maintain_near_term(self) -> dict[str, int]:
        rows = self._store.get_near_all()
        now = time.time()
        expired_ids: list[str] = []
        completed_promoted = 0
        completed_ids: list[str] = []

        for r in rows:
            meta = r["metadata"]
            # Completed → promote to long-term as a record of "we did this."
            if meta.get("completed"):
                entry = LongTermEntry(
                    content=f"Completed intent: {r['content']}",
                    topic="completed_intents",
                    evidence_count=1,
                    source=Source.CONSOLIDATOR,
                )
                self._store.add_long(entry)
                completed_promoted += 1
                completed_ids.append(r["id"])
                continue
            # Expired → drop.
            exp = meta.get("expires_at")
            if isinstance(exp, (int, float)) and exp > 0 and now > exp:
                expired_ids.append(r["id"])

        if completed_ids:
            self._store.delete_near(completed_ids)
        if expired_ids:
            self._store.delete_near(expired_ids)
            self._log(f"dropped {len(expired_ids)} expired near-term intents")

        return {"expired": len(expired_ids), "completed_promoted": completed_promoted}

    # ──────────────────────────────────────────────────────────────────
    #  Model plumbing
    # ──────────────────────────────────────────────────────────────────
    def _call_model(self, prompt: str, max_tokens: int = 200) -> str:
        """Call the configured OpenAI-compatible chat endpoint. Synchronous
        (consolidation runs in its own thread/process, not the event loop)."""
        cfg = self._cfg.consolidator
        url = cfg.model_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": cfg.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.3,  # low - we want consistent consolidation, not creativity
        }
        with httpx.Client(timeout=cfg.model_timeout_seconds) as client:
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
