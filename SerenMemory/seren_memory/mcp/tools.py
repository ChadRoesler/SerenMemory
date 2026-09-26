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

  The sleep (SerenHippocampus drafts; the main model reviews):
    submit_brief                what mattered - it opens the next sleep
    list_drafts                the review queue: the hippocampus's drafts
    get_draft                  one draft, every operation, every verdict
    review_draft               approve or deny each operation, with a critique
    get_satellites              a core's surroundings
    purge_memory_now            the emergency door (a flag, executed now)
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from ..collections import MemoryStore
from ..config import MemoryConfig
from ..models.schemas import (
    DailyBrief,
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

    Consolidation belongs to SerenHippocampus: nothing here drafts; the
    review tools apply what the main model approves.
    """

    def __init__(self, store: MemoryStore, config: MemoryConfig) -> None:
        self.store = store
        self.config = config

    # -- Core memory ------------------------------------------------------
    def remember(self, content: str, topic: Optional[str] = None) -> dict:
        """Write a short-term memory. The default tier - use this for
        anything you might want to recall later in this session or have
        the hippocampus draft into long-term if it recurs.

        GRANULARITY IS THE WHOLE GAME. Write ONE single-subject episode
        per call - one fact, one decision, one moment - and multi-topic
        TAG it via `topic` (comma-separated). Do NOT dump session-summary
        blobs: synthesising many topics into one summary is the
        CONSOLIDATOR's job, and that summary belongs in long-term, where
        summary-grain lives.

        Why this is a rule, not a style note: a blob's embedding is the
        average of its topics - a mushy centroid about nothing, that
        every query half-matches and nothing retrieves cleanly. Proven
        live on this store - an atomic single-subject entry retrieved at
        0.83 with a clean gap; the identical content buried in a
        ~1500-char blob smeared to the 0.53 floor. Atomic in, blob out.

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
               include_superseded: bool = False,
               with_surroundings: bool = False) -> dict:
        """Search memory for relevant context. The main retrieval path -
        call this before answering anything that might benefit from past
        context. Returns ranked hits across the requested tiers.

        Short-term is weighted highest (most recent context), long-term
        gets an evidence-count multiplier so well-established facts
        outrank passing mentions.

        Every long-term CORE hit carries `surroundings`: how many satellite
        episodes stand behind it, when the latest landed, and what it
        superseded / was superseded by - so you know there is a story even
        when you only asked for the answer. with_surroundings=true brings the
        most recent satellites and the superseded core along in full; for
        the whole set, get_satellites(core_id).
        """
        req = SearchRequest(
            query=query,
            n_results=n_results,
            with_surroundings=with_surroundings,
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

    def get_memory(self, memory_id: str) -> dict:
        """Hydrate ONE memory by its id - the dereference for a pointer
        `recall` handed back. recall returns an id alongside every hit; pass
        it here to pull the WHOLE entry (full content + metadata + which tier
        it lives in) when an idea needs a closer look or you want the context
        around it, instead of re-searching and hoping it ranks again. This is
        the right-brain twin of Loci's get_fact: exact lookup by handle, not
        ranked similarity. Returns {ok: false} if no recall tier holds that id.
        """
        row = self.store.get_by_id(memory_id)
        if row is None:
            return {"ok": False, "error": f"no memory '{memory_id}' in short/near/long"}
        return {
            "ok": True,
            "id": row["id"],
            "tier": row["tier"],
            "content": row["content"],
            "topic": row["metadata"].get("topic"),
            "metadata": row["metadata"],
        }

    # -- Open loops (near-term) -------------------------------------------
    def remember_for_later(self, intent: str,
                           trigger_type: str = "always",
                           trigger_value: Optional[str] = None,
                           expires_at: Optional[float] = None,
                           topic: Optional[str] = None) -> dict:
        """Write a future-tense intent - 'bring this up later', 'do X
        next time', 'check Y after Z'. Lives until completed or expired.

        Same granularity discipline as `remember`: ONE intent per call,
        multi-topic tagged - never a bundled to-do list. A single-subject
        intent fires cleanly when its trigger comes round; a blob of five
        loosely-related intents embeds to mush and surfaces for none of
        them. One loop, one entry.

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
        """Mark a short-term entry to be kept word for word: the hippocampus
        proposes it as a verbatim core in its next draft instead of
        synthesising it. Use when the words matter, not just the gist (a
        specific quote, a precise spec).

        Also pins the entry so it survives aging until that sleep.
        """
        ok = self.store.update_short_metadata(short_id, {
            "verbatim": True, "pinned": True,
        })
        if not ok:
            return {"ok": False, "error": f"no short-term entry '{short_id}'"}
        return {"ok": True, "id": short_id, "verbatim": True, "pinned": True}

    def promote_memory_now(self, short_id: str) -> dict:
        """Immediately move a short-term entry to long-term verbatim,
        skipping the sleep and its draft. 'I know this is durable, don't
        make me wait' override. The one way into long-term that is not an
        approved draft operation - direct POST /long is correctly
        forbidden; this is the agent-side escape hatch.
        """
        long_id = self.store.promote_short_to_long(short_id)
        if long_id is None:
            return {"ok": False, "error": f"no short-term entry '{short_id}'"}
        return {"ok": True, "long_term_id": long_id, "removed_short_id": short_id}

    def forget_memory(self, long_id: str, reason: str) -> dict:
        """Flag a long-term memory to be PURGED: the hippocampus executes
        the flag on its next tick - the core, its satellites and its sources
        go, and a tombstone (id, reason, time, never the content) stays.
        Never a surgical delete - the Lacuna gate is a flag, not a scalpel.
        For the emergency (a leaked secret) purge_memory_now does it at once.

        Use when the user asks to forget something. A fact that is merely
        wrong or outdated is NOT forgotten: remember the correction and let
        the next draft supersede the old core, which stays in history.
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
        """Submit a brief: it opens the hippocampus's next sleep and steers
        its draft. No brief, no sleep.

        summary: free-text 'what mattered this period' - the drafting model
                 reads it beside each cluster
        promote_hints: topic phrases worth remembering durably (a matching
                       cluster is drafted even from a single entry)
        noise_hints: topic phrases that are noise (a matching cluster is
                     not drafted unless the person pinned it)
        completed_intents: near-term intents that look done

        The hints are how a running bit becomes a core and a one-off
        becomes nothing - they ARE the lever for steering what becomes
        long-term. Use them.
        """
        brief = DailyBrief(
            summary=summary,
            promote_hints=promote_hints or [],
            noise_hints=noise_hints or [],
            completed_intents=completed_intents or [],
        )
        saved = self.store.add_brief(brief)
        return {"ok": True, "id": saved.id}

    def list_drafts(self, status: Optional[str] = "pending", limit: int = 20) -> dict:
        """The review queue. A draft is what the hippocampus proposes after
        a sleep: a list of operations on long-term (new_core, attach,
        supersede, verbatim), each reviewed on its own. status=pending is
        the queue; reviewed is history; None is everything.
        """
        rows = self.store.list_drafts(status=status, limit=limit)
        return {"count": len(rows), "drafts": [d.model_dump() for d in rows]}

    def get_draft(self, draft_id: str) -> dict:
        """One draft with every operation, its rationale, and its verdict
        so far. Read this before review_draft."""
        d = self.store.get_draft(draft_id)
        if d is None:
            return {"ok": False, "error": f"no draft '{draft_id}'"}
        return d.model_dump()

    def review_draft(self, draft_id: str, decisions: list[dict],
                      note: Optional[str] = None) -> dict:
        """Approve or deny operations, one verdict each.

        decisions: [{"op": 0, "verdict": "approve"},
                    {"op": 1, "verdict": "deny", "critique": "conflates X with Y; separate them"}]

        Approving APPLIES the operation now: a new core, a satellite attached
        to a core (and the core's evidence grows), a supersession (the old
        core stays, demoted), or a verbatim core. Denying records the
        critique; the hippocampus redrafts that operation and resubmits.
        Critiques should be specific - the next attempt is written from
        them. edited_content on an approval is accepted only on a terminal
        draft (the last permitted attempt); otherwise deny and let the loop
        do its job.
        """
        from ..draft import DraftError
        try:
            return {"ok": True, **self.store.review_draft(draft_id, decisions, note=note)}
        except KeyError:
            return {"ok": False, "error": f"no draft '{draft_id}'"}
        except DraftError as e:
            return {"ok": False, "error": str(e)}

    def purge_memory_now(self, memory_id: str, reason: str,
                         purge_backups: bool = True) -> dict:
        """The emergency door. Execute a purge NOW instead of at the next
        sleep: the entry, its satellites, its source short-terms wherever
        they sit, drafts that became it, the draft operations that touched
        it (scrubbed), and by default every migration backup beside the
        store. Leaves a tombstone with the id, the reason and what was
        removed - never the content.

        This is for "I pasted you my SSH key and it got committed". It is
        not how a fact gets corrected - a corrected fact is a new memory
        that supersedes the old one through a draft.
        """
        if not reason or not reason.strip():
            return {"ok": False, "error": "a reason is required to purge"}
        tomb = self.store.purge_long(memory_id, reason.strip(), purge_backups=purge_backups)
        if tomb is None:
            return {"ok": False, "error": f"no long-term entry '{memory_id}'"}
        return {"ok": True, "tombstone": tomb}

    def audit_drafts(self, limit: int = 10, since: Optional[float] = None) -> dict:
        """Every sleep's chain end to end, and how each model is doing.

        chains: the brief that opened it, every attempt, each operation's
        verdict, critique and any edit on approval, what landed. models:
        per consolidator model (as stamped on each draft) - first-pass
        approval rate, attempts to land, repeated denials (a critique that
        did not take), edits on approval, chains that ended denied.

        For catching drift in the small model, and for judging a model swap
        on numbers instead of a feeling. since: epoch seconds.
        """
        from ..audit import build_audit
        return build_audit(self.store, limit=limit, since=since)

    def get_satellites(self, core_id: str) -> dict:
        """The surroundings of a core: its supporting episodes with their
        dates, and the core it superseded. The draft around a hit."""
        core = self.store.get_by_id(core_id)
        if core is None or core.get("tier") != "long":
            return {"ok": False, "error": f"no long-term entry '{core_id}'"}
        sats = self.store.satellites_of(core_id)
        sup = core["metadata"].get("supersedes")
        return {"core": core, "satellites": sats, "count": len(sats),
                "supersedes": self.store.get_by_id(sup) if sup else None}

# ═══════════════════════════════════════════════════════════════════════
#  Registration entry point
# ═══════════════════════════════════════════════════════════════════════
def register_tools(mcp: FastMCP, store: MemoryStore, config: MemoryConfig) -> MemoryToolImpl:
    """Attach every MemoryToolImpl method to the given FastMCP instance
    via the @mcp.tool() decorator. Returns the impl object so callers
    that need a handle (e.g. for direct invocation in tests at the seam,
    can keep one.
    """
    impl = MemoryToolImpl(store, config)

    # Core memory
    mcp.tool()(impl.remember)
    mcp.tool()(impl.recall)
    mcp.tool()(impl.what_do_you_remember)
    mcp.tool()(impl.get_memory)

    # Open loops
    mcp.tool()(impl.remember_for_later)
    mcp.tool()(impl.complete_intent)

    # Agency surface
    mcp.tool()(impl.preserve_memory_verbatim)
    mcp.tool()(impl.promote_memory_now)
    mcp.tool()(impl.forget_memory)

    # The sleep: the brief that opens it, the drafts it proposes
    mcp.tool()(impl.submit_brief)
    # Drafts + the surroundings + the emergency door
    mcp.tool()(impl.list_drafts)
    mcp.tool()(impl.get_draft)
    mcp.tool()(impl.review_draft)
    mcp.tool()(impl.get_satellites)
    mcp.tool()(impl.audit_drafts)
    mcp.tool()(impl.purge_memory_now)

    return impl
