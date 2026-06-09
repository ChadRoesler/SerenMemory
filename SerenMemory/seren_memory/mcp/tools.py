"""
seren_memory.mcp.tools
══════════════════════

The tools the MCP server exposes. Each tool is a thin wrapper over
MemoryStore (in-process) - we're mounted INTO the same FastAPI app that
owns the store, so there's no point in HTTP-round-tripping ourselves.

STRUCTURE

`MemoryToolImpl` holds every tool as a method. `register_tools` wires
each method onto a FastMCP instance via the `@mcp.tool()` decorator. The
split exists for testability - `MemoryToolImpl(...).remember(...)` is
directly callable in unit tests without going through FastMCP, an MCP
client, or an HTTP roundtrip. See `tests/test_mcp_tools.py`.

TOOL ROSTER (organised by what they do, not API path):

  Core memory:
    remember                    write to short-term
    recall                      search across tiers (the main retrieval path)
    what_do_you_remember        list recent short-term (debug / reflection)

  Open loops (near-term):
    remember_for_later          write a future intent
    complete_intent             mark an intent as acted-on

  Agency surface:
    preserve_memory_verbatim    mark a short entry for verbatim peel-off
    promote_memory_now          immediate verbatim promotion to long-term
    forget_memory               the Lacuna gate on long-term

  Brief + consolidation:
    submit_brief                steering payload for next consolidator cycle
    consolidate_now             trigger a cycle (requires consolidator model)

  Draft review (when a consolidator model IS configured):
    list_drafts                 the review queue
    get_draft_chain             all attempts in a redraft chain
    approve_draft               commit a pending draft
    reject_draft                send critique, trigger redraft
    select_draft                commit best from chain, optionally edited

  Self-consolidation (when no consolidator model is configured):
    prepare_consolidation       returns unconsolidated shorts + a synthesis
                                template - the ACTIVE model does the synthesis
                                in its own reasoning
    commit_consolidation        model sends back its synthesised draft;
                                server persists as pending draft
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..collections import MemoryStore
from ..config import MemoryConfig
from ..consolidator.service import Consolidator, ConsolidatorBusy
from ..models.schemas import (
    DailyBrief,
    DraftEntry,
    NearTermEntry,
    SearchRequest,
    ShortTermEntry,
    Source,
    TriggerType,
)


# Tier weights duplicated from routes/search.py to keep recall logic
# fully in-process. Refactor target if these ever diverge - for now,
# small enough to inline rather than couple the modules.
_TIER_WEIGHT = {"short": 1.0, "near": 0.9, "long": 0.8}


class MemoryToolImpl:
    """The actual tool implementations, callable both via FastMCP
    decoration (in production) and directly (in unit tests).

    Each method's return shape is JSON-serialisable - the FastMCP layer
    serialises it on the way out to the MCP client.

    consolidator may be None on deploys with no local LLM. Tools that
    require it (consolidate_now, reject_draft's redraft step) return a
    helpful error in that case rather than crashing with AttributeError.
    """

    def __init__(self, store: MemoryStore, config: MemoryConfig,
                 consolidator: Optional[Consolidator]) -> None:
        self.store = store
        self.config = config
        self.consolidator = consolidator

    # -- Core memory ------------------------------------------------------
    def remember(self, content: str, topic: Optional[str] = None) -> dict:
        """Write a short-term memory. The default tier - use this for
        anything you might want to recall later in this session or have
        the consolidator promote to long-term if it recurs.

        For exact-phrasing-matters facts, follow up with
        preserve_memory_verbatim on the returned id.
        """
        entry = ShortTermEntry(content=content, topic=topic)
        saved = self.store.add_short(entry)
        return {"ok": True, "id": saved.id, "tier": "short"}

    def recall(self, query: str, n_results: int = 5,
               include_short: bool = True,
               include_near: bool = True,
               include_long: bool = True,
               include_superseded: bool = False) -> dict:
        """Search memory for relevant context. The main retrieval path -
        call this before answering anything that might benefit from past
        context. Returns ranked hits across the requested tiers.

        Short-term is weighted highest (most recent context), long-term
        gets an evidence-count multiplier so well-established facts
        outrank passing mentions.
        """
        req = SearchRequest(
            query=query,
            n_results=n_results,
            include_short=include_short,
            include_near=include_near,
            include_long=include_long,
            include_superseded=include_superseded,
        )
        searched: list[str] = []
        all_hits: list[dict] = []
        fetch_n = req.n_results * 2

        tiers = []
        if req.include_short: tiers.append("short")
        if req.include_near: tiers.append("near")
        if req.include_long: tiers.append("long")

        for tier in tiers:
            searched.append(tier)
            try:
                raw = self.store.query(tier, req.query, fetch_n)
            except ValueError:
                continue
            for hit in raw:
                meta = hit["metadata"]
                if tier == "long" and not req.include_superseded and meta.get("superseded_by"):
                    continue
                if tier == "near" and meta.get("completed"):
                    continue
                distance = hit["distance"]
                base = 1.0 / (1.0 + max(distance, 0.0))
                score = base * _TIER_WEIGHT[tier]
                if tier == "long":
                    ev = meta.get("evidence_count", 1)
                    if isinstance(ev, (int, float)) and ev > 0:
                        score *= (1.0 + math.log(ev) * 0.15)
                all_hits.append({
                    "tier": tier,
                    "content": hit["content"],
                    "topic": meta.get("topic"),
                    "score": round(score, 6),
                    "id": hit["id"],
                })
        all_hits.sort(key=lambda h: h["score"], reverse=True)
        return {
            "query": req.query,
            "hits": all_hits[:req.n_results],
            "searched_tiers": searched,
        }

    def what_do_you_remember(self, limit: int = 20,
                             topic: Optional[str] = None) -> dict:
        """List recent short-term entries (debug / self-reflection). Filter
        by topic if given. Newest first.

        NOT a recall - use 'recall' for relevance-ranked search. This is
        the inventory view: 'what's been written down recently.'
        """
        rows = self.store.get_short_all(limit=None)
        if topic:
            rows = [r for r in rows if r["metadata"].get("topic") == topic]
        rows.sort(key=lambda r: r["metadata"].get("ts", 0), reverse=True)
        rows = rows[:limit]
        return {
            "count": len(rows),
            "entries": [
                {"id": r["id"],
                 "content": r["content"],
                 "topic": r["metadata"].get("topic"),
                 "ts": r["metadata"].get("ts")}
                for r in rows
            ],
        }

    # -- Open loops (near-term) -------------------------------------------
    def remember_for_later(self, intent: str,
                           trigger_type: str = "always",
                           trigger_value: Optional[str] = None,
                           expires_at: Optional[float] = None,
                           topic: Optional[str] = None) -> dict:
        """Write a future-tense intent - 'bring this up later', 'do X
        next time', 'check Y after Z'. Lives until completed or expired.

        trigger_type: 'time' (trigger_value = unix ts after which it's due),
        'event' (trigger_value = match string like 'mentions:balatro'), or
        'always' (standing note - surfaces on any relevant query).
        """
        try:
            tt = TriggerType(trigger_type)
        except ValueError:
            return {"ok": False, "error":
                    f"trigger_type must be one of: time, event, always (got {trigger_type!r})"}
        entry = NearTermEntry(
            intent=intent, topic=topic,
            trigger_type=tt, trigger_value=trigger_value,
            expires_at=expires_at,
        )
        saved = self.store.add_near(entry)
        return {"ok": True, "id": saved.id, "tier": "near"}

    def complete_intent(self, intent_id: str) -> dict:
        """Mark a near-term intent as ACTED ON (not merely referenced).
        Consolidator promotes completed intents to long-term as a record
        of 'we did this.' Without this, intents accumulate forever.
        """
        ok = self.store.update_near(intent_id, {
            "completed": True,
            "completed_at": time.time(),
        })
        if not ok:
            return {"ok": False, "error": f"no near-term entry '{intent_id}'"}
        return {"ok": True, "completed": intent_id}

    # -- Agency surface ---------------------------------------------------
    def preserve_memory_verbatim(self, short_id: str) -> dict:
        """Mark a short-term entry for verbatim promotion on the next
        consolidator cycle - preserves exact phrasing instead of having
        the consolidator synthesise. Use when the words matter, not just
        the gist (a specific quote, a precise spec).

        Also pins the entry so it survives aging until the cycle runs.
        """
        ok = self.store.update_short_metadata(short_id, {
            "verbatim": True, "pinned": True,
        })
        if not ok:
            return {"ok": False, "error": f"no short-term entry '{short_id}'"}
        return {"ok": True, "id": short_id, "verbatim": True, "pinned": True}

    def promote_memory_now(self, short_id: str) -> dict:
        """Immediately move a short-term entry to long-term verbatim,
        skipping the consolidator cycle. 'I know this is durable, don't
        make me wait' override. The right tool when you need to add to
        long-term right now - direct POST /long is correctly forbidden
        (consolidator owns long-term writes); this is the agent-side
        escape hatch.
        """
        long_id = self.store.promote_short_to_long(short_id)
        if long_id is None:
            return {"ok": False, "error": f"no short-term entry '{short_id}'"}
        return {"ok": True, "long_term_id": long_id, "removed_short_id": short_id}

    def forget_memory(self, long_id: str, reason: str) -> dict:
        """Flag a long-term memory for the consolidator's attention. PII
        keywords trigger purge; other reasons demote the entry (zero
        evidence_count, marks demoted_reason). Never a surgical delete -
        the Lacuna gate is a flag, not a scalpel.

        Use when the user asks to forget something OR when you discover
        a long-term fact is wrong / outdated.
        """
        if not reason or not reason.strip():
            return {"ok": False, "error": "reason is required to flag for forget"}
        ok = self.store.flag_long_forget(long_id, reason)
        if not ok:
            return {"ok": False, "error": f"no long-term entry '{long_id}'"}
        return {"ok": True, "id": long_id, "flagged_reason": reason}

    # -- Brief + consolidation --------------------------------------------
    def submit_brief(self, summary: str,
                     promote_hints: Optional[list[str]] = None,
                     noise_hints: Optional[list[str]] = None,
                     completed_intents: Optional[list[str]] = None) -> dict:
        """Submit a steering brief for the next consolidator cycle.

        summary: free-text 'what mattered this period'
        promote_hints: topic phrases worth remembering durably (lowers
                       cluster-promotion threshold for matching entries)
        noise_hints: topic phrases that are noise (raises threshold)
        completed_intents: near-term intents that look done

        The hints get matched against the haystack of topics + content
        during cluster promotion - they ARE the lever for steering what
        becomes long-term. Use them.
        """
        brief = DailyBrief(
            summary=summary,
            promote_hints=promote_hints or [],
            noise_hints=noise_hints or [],
            completed_intents=completed_intents or [],
        )
        saved = self.store.add_brief(brief)
        return {"ok": True, "id": saved.id}

    def consolidate_now(self) -> dict:
        """Trigger a consolidation cycle right now. Synchronous - returns
        when the cycle finishes.

        REQUIRES a configured consolidator model (Nemotron-style local
        LLM, or whatever model_url in config points at). Without one,
        synthesis falls back to mechanical (longest entry per cluster).
        For deploys with no local LLM, prefer the self-consolidation
        pair: prepare_consolidation + commit_consolidation.
        """
        if self.consolidator is None:
            return {"ok": False, "error": "consolidator not configured on this deployment"}
        try:
            report = self.consolidator.run_once()
        except ConsolidatorBusy as e:
            return {"ok": False, "error": f"consolidator busy: {e}"}
        return {"ok": True, "report": report}

    # -- Draft review -----------------------------------------------------
    def list_drafts(self, status: Optional[str] = None,
                    limit: int = 20) -> dict:
        """List consolidator drafts (the model review queue). Filter by
        status: pending, approved, rejected, requires_selection. Omit
        for all. Newest first.
        """
        rows = self.store.get_recent_drafts(limit=limit, status=status)
        return {"count": len(rows), "entries": rows}

    def get_draft_chain(self, draft_id: str) -> dict:
        """Return all synthesis attempts for the same cluster as draft_id,
        ordered by attempt number. Use this when a draft is in
        requires_selection state - compare all attempts before selecting
        the best via select_draft.
        """
        row = self.store._get_draft_row(draft_id)
        if not row:
            return {"ok": False, "error": f"no draft '{draft_id}'"}
        cluster_id = row["metadata"].get("cluster_id", draft_id)
        chain = self.store.get_drafts_by_cluster(cluster_id)
        return {"cluster_id": cluster_id, "attempts": chain, "count": len(chain)}

    def approve_draft(self, draft_id: str,
                      note: Optional[str] = None) -> dict:
        """Approve a pending draft - commits to long-term and archives
        source shorts to pruned. Optional note recorded with approval.
        """
        result = self.store.approve_draft(draft_id, note=note)
        if result is None:
            existing = self.store._get_draft_row(draft_id)
            if not existing:
                return {"ok": False, "error": f"no draft '{draft_id}'"}
            return {"ok": False, "error":
                    f"draft '{draft_id}' already "
                    f"{existing['metadata'].get('status')}"}
        return {"ok": True, "draft_id": draft_id, **result}

    def reject_draft(self, draft_id: str, critique: str) -> dict:
        """Reject a draft with a critique. The consolidator will redraft
        (up to max_redraft_attempts). Once exhausted, the chain flips
        to requires_selection - at which point use select_draft.

        Critique should be SPECIFIC ('conflated X with Y; separate them
        and emphasise Y') not generic ('wrong vibe'). The next attempt
        sees the critique + all previous attempts as steering.
        """
        if not critique or not critique.strip():
            return {"ok": False, "error": "critique is required to reject"}
        if self.consolidator is None:
            return {"ok": False, "error":
                    "redraft requires a configured consolidator model"}
        cluster_meta = self.store.reject_draft(draft_id, critique)
        if cluster_meta is None:
            existing = self.store._get_draft_row(draft_id)
            if not existing:
                return {"ok": False, "error": f"no draft '{draft_id}'"}
            return {"ok": False, "error":
                    f"draft '{draft_id}' already "
                    f"{existing['metadata'].get('status')}"}
        redraft_result = self.consolidator.redraft_cluster(
            cluster_id=cluster_meta["cluster_id"],
            rejected_draft_id=draft_id,
            critique=critique,
            attempt=cluster_meta["attempt"],
            source_short_ids=cluster_meta["source_short_ids"],
            brief_id_used=cluster_meta["brief_id_used"],
            topic=cluster_meta["topic"],
            evidence_count=cluster_meta["evidence_count"],
        )
        if redraft_result is None:
            return {"ok": True, "draft_id": draft_id, "action": "rejected",
                    "warning": "redraft synthesis failed; cluster stays in pool"}
        return {"ok": True, "draft_id": draft_id,
                "action": redraft_result["action"],
                "new_draft_id": redraft_result.get("draft_id"),
                "attempt": redraft_result["attempt"]}

    def select_draft(self, draft_id: str,
                     edited_content: Optional[str] = None,
                     note: Optional[str] = None) -> dict:
        """Commit the best attempt from a requires_selection chain. Marks
        siblings rejected, archives source shorts.

        edited_content: optional revised text to commit INSTEAD of the
        draft. Use when none of the chain attempts is quite right but
        one is closest - pick that one + send the polished version. The
        original draft text is preserved on the draft row for audit;
        long-term gets the edit.
        """
        if edited_content is not None and not edited_content.strip():
            return {"ok": False, "error":
                    "edited_content must be non-empty; omit to commit as-is"}
        result = self.store.select_draft(draft_id, note=note,
                                         edited_content=edited_content)
        if result is None:
            existing = self.store._get_draft_row(draft_id)
            if not existing:
                return {"ok": False, "error": f"no draft '{draft_id}'"}
            status = existing["metadata"].get("status")
            return {"ok": False, "error":
                    f"draft '{draft_id}' is '{status}', not requires_selection"}
        return {"ok": True, "draft_id": draft_id, **result}

    # -- Self-consolidation (model-as-consolidator) -----------------------
    def prepare_consolidation(self, max_entries: int = 50) -> dict:
        """Return unconsolidated short-term entries + a synthesis prompt
        template. The CALLING MODEL does the synthesis in its own
        reasoning, then calls commit_consolidation with the result.

        Use when no dedicated consolidator model is available - the
        active model becomes the consolidator. Pairs with
        commit_consolidation. Entries grouped by topic so you can pick
        a cluster to synthesise.
        """
        rows = self.store.get_short_all(limit=None)
        if not rows:
            return {"ok": True, "clusters": {}, "prompt_template": None,
                    "note": "no short-term entries to consolidate"}
        rows = rows[-max_entries:]
        clusters: dict[str, list[dict]] = {}
        for r in rows:
            topic = r["metadata"].get("topic") or "_untagged"
            clusters.setdefault(topic, []).append({
                "id": r["id"],
                "content": r["content"],
                "ts": r["metadata"].get("ts"),
            })

        prompt_template = (
            "You are consolidating short-term memory entries into one durable "
            "long-term statement.\n\n"
            "Topic: {topic}\n\n"
            "Fragments:\n{fragments}\n\n"
            "Produce ONE concise, durable statement of fact - present tense, "
            "no preamble, no 'the user said'. Just the consolidated truth.\n\n"
            "Then call commit_consolidation with:\n"
            "  draft_text: your synthesised statement\n"
            "  source_short_ids: the ids of the fragments you used\n"
            "  topic: the cluster topic\n"
        )
        return {
            "ok": True,
            "clusters": clusters,
            "prompt_template": prompt_template,
            "note": "synthesise one cluster at a time; call commit_consolidation per cluster",
        }

    def commit_consolidation(self, draft_text: str,
                             source_short_ids: list[str],
                             topic: Optional[str] = None) -> dict:
        """Persist a model-synthesised consolidation draft. Lands as
        pending in the draft queue - a stronger reviewer (different
        model, later session) can review normally; if none ever does,
        the draft sits as candidate evidence.

        Pairs with prepare_consolidation. The draft does NOT auto-commit
        to long-term - single-model self-review is too weak a gate.
        """
        if not draft_text or not draft_text.strip():
            return {"ok": False, "error": "draft_text required"}
        if not source_short_ids:
            return {"ok": False, "error":
                    "source_short_ids required (which shorts informed this draft)"}
        draft = DraftEntry(
            content=draft_text.strip(),
            topic=topic,
            evidence_count=len(source_short_ids),
            source_short_ids=list(source_short_ids),
            source=Source.ASSISTANT,
            attempt=1,
        )
        draft.cluster_id = draft.id
        saved = self.store.add_draft(draft)
        return {
            "ok": True,
            "draft_id": saved.id,
            "status": "pending",
            "note": "draft queued for review; will commit to long-term on approve/select",
        }


# ═══════════════════════════════════════════════════════════════════════
#  Registration entry point
# ═══════════════════════════════════════════════════════════════════════
def register_tools(mcp: FastMCP, store: MemoryStore, config: MemoryConfig,
                   consolidator: Optional[Consolidator]) -> MemoryToolImpl:
    """Attach every MemoryToolImpl method to the given FastMCP instance
    via the @mcp.tool() decorator. Returns the impl object so callers
    that need a handle (e.g. for direct invocation in tests at the seam,
    or for graceful shutdown that touches the consolidator) can keep one.
    """
    impl = MemoryToolImpl(store, config, consolidator)

    # Core memory
    mcp.tool()(impl.remember)
    mcp.tool()(impl.recall)
    mcp.tool()(impl.what_do_you_remember)

    # Open loops
    mcp.tool()(impl.remember_for_later)
    mcp.tool()(impl.complete_intent)

    # Agency surface
    mcp.tool()(impl.preserve_memory_verbatim)
    mcp.tool()(impl.promote_memory_now)
    mcp.tool()(impl.forget_memory)

    # Brief + consolidation
    mcp.tool()(impl.submit_brief)
    mcp.tool()(impl.consolidate_now)

    # Draft review
    mcp.tool()(impl.list_drafts)
    mcp.tool()(impl.get_draft_chain)
    mcp.tool()(impl.approve_draft)
    mcp.tool()(impl.reject_draft)
    mcp.tool()(impl.select_draft)

    # Self-consolidation
    mcp.tool()(impl.prepare_consolidation)
    mcp.tool()(impl.commit_consolidation)

    return impl
