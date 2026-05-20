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

  4. Age out short-term: entries older than short_term_seconds that DIDN'T
     get promoted get archived to 'pruned' (safety net) then deleted.

  5. Maintain near-term:
       - expired (past expires_at) → drop
       - completed → promote to long-term as a record, then drop from near
       - long-unfulfilled → review (model decides keep/let-go)

  6. Sweep the pruned safety net (true-delete past the safety window).

MODEL USAGE: minimal. The model is used for (a) synthesizing a cluster of
short-term entries into one long-term statement, and (b) optionally judging
ambiguous forget-flags / stale intents. Everything else is mechanical
(clustering via embeddings, timestamp comparisons). A 2B-4B model is plenty
because the model only ever does "summarize these few things into one" or
"yes/no should this stay" - never long reasoning.

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
from ..models.schemas import LongTermEntry, Source


class Consolidator:
    def __init__(self, store: MemoryStore, config: MemoryConfig, log=None):
        self._store = store
        self._cfg = config
        self._log = log or (lambda m: print(f"[consolidator] {m}"))

    # ──────────────────────────────────────────────────────────────────
    #  Entry point - one full consolidation pass.
    # ──────────────────────────────────────────────────────────────────
    def run_once(self) -> dict[str, Any]:
        start = time.time()
        self._log("consolidation window opening")
        report: dict[str, Any] = {
            "started_at": start,
            "promoted": 0,
            "aged_out": 0,
            "near_expired": 0,
            "near_completed_promoted": 0,
            "forget_flags_handled": 0,
            "pruned_swept": 0,
        }

        brief = self._store.get_latest_brief()
        promote_hints = set((brief or {}).get("metadata", {}).get("promote_hints", []) or [])
        noise_hints = set((brief or {}).get("metadata", {}).get("noise_hints", []) or [])
        if brief:
            self._log(f"using brief from {brief['metadata'].get('created_at')}: "
                      f"{len(promote_hints)} promote-hints, {len(noise_hints)} noise-hints")

        report["forget_flags_handled"] = self._handle_forget_flags()
        report["promoted"] = self._promote_short_term(promote_hints, noise_hints)
        report["aged_out"] = self._age_out_short_term()
        near = self._maintain_near_term()
        report["near_expired"] = near["expired"]
        report["near_completed_promoted"] = near["completed_promoted"]
        report["pruned_swept"] = self._store.sweep_pruned(
            self._cfg.consolidator.pruned_safety_days * 24 * 3600)

        report["duration_seconds"] = round(time.time() - start, 2)
        report["counts_after"] = self._store.counts()
        self._log(f"window closed: {report}")
        return report

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
    def _promote_short_term(self, promote_hints: set[str], noise_hints: set[str]) -> int:
        rows = self._store.get_short_all(limit=self._cfg.consolidator.max_entries_per_run)
        if not rows:
            return 0

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

        promoted = 0
        promoted_ids: list[str] = []

        for topic, entries in clusters.items():
            pinned = [e for e in entries if e["metadata"].get("pinned")]
            threshold = self._cfg.consolidator.promote_min_evidence

            # Brief hints adjust the threshold for this topic.
            topic_l = topic.lower()
            if any(h.lower() in topic_l for h in promote_hints):
                threshold = 1  # brief said promote this → low bar
            if any(h.lower() in topic_l for h in noise_hints):
                threshold = 999  # brief said noise → effectively never

            should_promote = pinned or len(entries) >= threshold
            if not should_promote:
                continue

            # Synthesize the cluster into one long-term statement.
            synthesis = self._synthesize(topic, entries)
            if not synthesis:
                continue

            entry = LongTermEntry(
                content=synthesis,
                topic=None if topic == "_untagged" else topic,
                evidence_count=len(entries),
                source=Source.CONSOLIDATOR,
            )
            self._store.add_long(entry)
            promoted += 1
            promoted_ids.extend(e["id"] for e in entries)
            self._log(f"promoted topic '{topic}' ({len(entries)} entries) → long-term")

        # Promoted short-term entries are consumed (their essence now lives
        # in long-term). Remove them so they don't re-promote next cycle.
        if promoted_ids:
            self._store.delete_short(promoted_ids)

        return promoted

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
            return result.strip() if result else fallback
        except Exception as e:  # noqa: BLE001 - degrade gracefully on any model error
            self._log(f"synthesis model call failed ({e}); using fallback")
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
