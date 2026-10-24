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
    recall                      search across tiers (the right brain alone; a
                                connected Corpus Callosum's `search` comes first)
    what_do_you_remember        list recent short-term (debug / reflection)

  Open loops (near-term):
    remember_for_later          write a future intent
    complete_intent             mark an intent as acted-on

  Agency surface:
    preserve_memory_verbatim    mark a short entry for verbatim peel-off
    release_memory_verbatim     take that mark back off
    promote_memory_now          immediate verbatim promotion to long-term
    forget_memory               the Lacuna gate on long-term
    retract_memory              take back a short-term I wrote (a duplicate, a mistake)

  The sleep (SerenHippocampus drafts; the main model reviews):
    submit_brief                what mattered - it opens the next sleep
    list_drafts                the review queue: the hippocampus's drafts
    get_draft                  one draft, every operation, every verdict
    review_draft               approve or deny each operation, with a critique
    undo_restate               flag a core to get its earlier wording back
    get_satellites              a core's surroundings
    where_else                  where else a core's or satellite's story might hang
    link_satellite              flag a satellite to hang on one more core
    unlink_satellite            flag a satellite to come off one of its cores
    fold_satellite              flag a satellite that is the same episode as another to fold into it
    purge_memory_now            the emergency door (a flag, executed now)
"""
from __future__ import annotations

import json
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
    def remember(self, content: str, topic: Optional[str] = None,
                 keep: Optional[list[str]] = None) -> dict:
        """Write a short-term memory. The default tier - use this for
        anything you might want to recall later in this session or have
        the hippocampus draft into long-term if it recurs.

        `keep`: pictures that ARE part of the memory - what you drew, what
        you saw. Paths on this box (the gallery make_image saves to) or
        URLs, under the shelves eidetic.sources names. They live and die
        with the entry: promoted, drafted into a core, archived, the
        pictures ride along; look_at_memory shows them. The answer lists
        what was kept and what was refused and why; the memory is written
        either way.

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
        out: dict[str, Any] = {"ok": True, "id": saved.id, "tier": "short"}
        if keep:
            out["kept"], out["refused"] = self._keep_many(saved.id, keep)
        return out

    def _keep_many(self, entry_id: str, refs: list[str], caption: Optional[str] = None
                   ) -> tuple[list[dict], list[dict]]:
        """Each reference fetched and kept with the entry; one that cannot
        be is listed with why, and the rest still land."""
        from seren_sinew.blobs import keep_each
        ks = self.config.eidetic
        return keep_each(refs, lambda data, ref: self.store.keep_with(entry_id, data, caption=caption, source=ref),
                         ks.sources, ks.max_bytes)

    # -- Eidetic: pictures kept with memories -----------------------------
    def keep_with_memory(self, memory_id: str, source: str, caption: Optional[str] = None) -> dict:
        """Keep a picture (or a clip, a sound, a PDF) with a memory you
        already wrote - in short, near or long.

        A picture can BE the memory: what you drew at 3 a.m., what the
        screen showed. Kept here it is part of the entry, in Memory's own
        folder under its hash and in the same snapshots, and it goes where
        the entry goes - promoted, drafted into a core, archived with it.
        The entry's words are what it is about; `caption` is a line more.

        `source`: a path on this box (make_image's gallery) or a URL, under
        the shelves eidetic.sources names. What is kept is decided by the
        bytes, never the filename. look_at_memory shows it back.
        """
        if self.store.get_by_id(memory_id) is None:
            return {"ok": False, "id": memory_id, "error": "no memory with that id in short/near/long"}
        kept, refused = self._keep_many(memory_id, [source], caption)
        return {"ok": bool(kept), "id": memory_id, "kept": kept, "refused": refused}

    # No return annotation, on purpose: with one, FastMCP tries to serialise
    # the answer as structured output and cannot serialise an Image.
    def look_at_memory(self, memory_id: str):
        """Look at a memory and the pictures kept with it - SHOWN, as
        images, not described. recall and get_memory tell you an entry has
        pictures (`eidetic`); this is how you see them.

        Pictures come back as image content beside the entry. Up to about
        8 MB per look; the rest, and anything that is not a still picture
        (a clip, a sound, a PDF), is listed under `not_shown` with the
        reason.
        """
        from mcp.server.fastmcp import Image
        from seren_sinew.blobs import choose_shown
        row = self.store.get_by_id(memory_id)
        if row is None:
            return {"ok": False, "id": memory_id, "error": "no memory with that id"}

        def read(k: dict) -> Optional[bytes]:
            got = self.store.open_eidetic(memory_id, k["digest"])
            if got is None:
                return None
            with got[1] as f:
                return f.read()
        rows = self.store.eidetic_in(row["metadata"])
        shown, not_shown = choose_shown(rows, read)
        text = {"ok": True, "id": row["id"], "tier": row["tier"], "content": row["content"],
                "topic": row["metadata"].get("topic"), "eidetic": rows, "shown": len(shown),
                "not_shown": [{k: v for k, v in r.items() if k in ("file", "mime", "bytes", "why")} for r in not_shown]}
        return [text, *(Image(data=data, format=fmt) for data, fmt in shown)] if shown else text

    def drop_from_memory(self, memory_id: str, digest: str) -> dict:
        """Take one picture off a memory. The entry stays; the picture's row
        goes, and its bytes go with it once no entry in any tier names
        them. Needs the picture's `digest` (from the entry's `eidetic`).
        """
        ok = self.store.drop_from(memory_id, digest)
        return {"ok": ok, "id": memory_id, "digest": digest,
                "note": "dropped" if ok else "no such picture on that memory"}

    def recall(self, query: str, n_results: int = 5,
               include_short: bool = True,
               include_near: bool = True,
               include_long: bool = True,
               include_superseded: bool = False,
               with_surroundings: bool = False) -> dict:
        """Search the right brain alone - episodic memory, across the tiers
        you ask for. Returns ranked hits.

        WHEN A CORPUS CALLOSUM IS CONNECTED, ITS `search` COMES FIRST: one call
        reaches this store AND the left brain's facts, merged and ranked, with
        each core's satellites and context riding along - a fuller picture
        for fewer tokens than calling here and search_loci both. Reach for
        `recall` when you specifically want only episodic memory, or one tier
        of it (a recent short-term, an open intent, a settled core). With no
        callosum installed, this IS the retrieval path.

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
                row = {
                    "tier": tier,
                    "content": hit["content"],
                    "topic": meta.get("topic"),
                    "score": round(score, 6),
                    "id": hit["id"],
                }
                if (n := len(self.store.eidetic_in(meta))):
                    row["eidetic"] = n                      # how many; look_at_memory shows them
                all_hits.append(row)
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
                 "ts": r["metadata"].get("ts"),
                 **({"eidetic": n} if (n := len(self.store.eidetic_in(r["metadata"]))) else {})}
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

        A short-term that an approved draft operation consumed is not gone
        at once: it sits in the pruned archive for pruned_safety_days and
        comes back here as tier "pruned" (recall does not see it). If an
        operation landed holding only part of a fragment, this is how to
        read the rest and write it back with remember.

        PRUNED IS NOT LOST. A pruned entry carries pruned_because: what sent
        it there - the operation that consumed it and what it became (the
        core, and the satellite that keeps its words), an age-out, or a
        retraction. Read it before writing the memory again: on 23 Oct 2026
        I took a landed episode for a lost one and wrote a duplicate.
        """
        row = self.store.get_by_id(memory_id)
        if row is None:
            return {"ok": False, "error": f"no memory '{memory_id}' in short/near/long, nor in the pruned archive"}
        out = {
            "ok": True,
            "id": row["id"],
            "tier": row["tier"],
            "content": row["content"],
            "topic": row["metadata"].get("topic"),
            "metadata": row["metadata"],
        }
        if row["tier"] == "pruned":
            out["pruned_because"] = _pruned_because(row["metadata"])
        return out

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

    def release_memory_verbatim(self, short_id: str, keep_pinned: bool = False) -> dict:
        """Take the verbatim mark back off a short-term entry - you marked
        it word for word and have changed your mind. The next sleep then
        drafts it like any other memory instead of proposing it verbatim.

        The pin that came with the mark is removed too, so the entry ages
        normally again; keep_pinned=true leaves it pinned. Only the mark
        changes: the entry and its text stay as they are. The mark is yours
        to give (preserve_memory_verbatim) and yours to take back.
        """
        row = self.store.get_by_id(short_id)
        if row is None or row.get("tier") != "short":
            return {"ok": False, "error": f"no short-term entry '{short_id}'"}
        if not (row.get("metadata") or {}).get("verbatim"):
            return {"ok": False, "error": f"short-term '{short_id}' is not marked verbatim"}
        self.store.update_short_metadata(short_id, {"verbatim": None, **({} if keep_pinned else {"pinned": None})})
        return {"ok": True, "id": short_id, "verbatim": False, "pinned": bool(keep_pinned)}

    def promote_memory_now(self, short_id: str) -> dict:
        """Immediately move a short-term entry to long-term verbatim,
        skipping the sleep and its draft. 'I know this is durable, don't
        make me wait' override. The one way into long-term that is not an
        approved draft operation - direct POST /long is correctly
        forbidden; this is the agent-side escape hatch.

        Not for a memory that is already under review. If a draft holds this
        entry (an operation citing it is pending, or was denied and its
        redraft has not come back), this refuses and names the draft: review
        is the gate, and a memory in review lands by its verdict there
        (review_draft), not around it. Once the draft's chain is finished the
        entry is free again.
        """
        held = self.store.shorts_under_review().get(short_id)
        if held:
            return {"ok": False, "held_by_draft": held,
                    "error": f"short-term '{short_id}' is under review in draft {held}: approve or deny its "
                             f"operation there (review_draft). A memory in review is promoted by the review, "
                             f"not around it."}
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

    def retract_memory(self, memory_id: str, reason: str) -> dict:
        """Take back a SHORT-TERM memory I wrote: a duplicate, a mistake,
        something written in the wrong words (write it again right, then
        retract the wrong one). It goes to the pruned archive with the
        reason - readable by id with get_memory until the sweep - and no
        draft will see it. Not for long-term (that is forget_memory), not
        for one a pending draft holds (decide it there), not for a pinned
        one (release_memory_verbatim first). A reason is required.
        """
        try:
            out = self.store.retract_short(memory_id, reason)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out}

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
        so far. Read this before review_draft.

        Each attach or supersede carries target_core: the core it would
        change, in full. An attach with restated_content carries
        restate_check - approving it REPLACES that core's wording, so compare
        the two. On a redraft, earlier_attempts shows every previous version
        of the operation and the critique that sent it back. An operation
        that would leave fragments in short-term carries sources_check and
        left_in_short_term (see review_draft). last_attempt is set when
        nothing comes back after this one."""
        view = self.store.review_view(draft_id)
        if view is None:
            return {"ok": False, "error": f"no draft '{draft_id}'"}
        return view

    def review_draft(self, draft_id: str, decisions: list[dict],
                      note: Optional[str] = None) -> dict:
        """Approve or deny operations, one verdict each.

        decisions: [{"op": 0, "verdict": "approve"},
                    {"op": 1, "verdict": "deny", "critique": "conflates X with Y; separate them"}]

        Approving APPLIES the operation now: a new core; an attach (SUPPORT:
        the memory itself becomes a satellite of the core, whose evidence
        grows - or a REFINE, with restated_content: a small change, the core
        reworded); a supersede (REPLACE: the old core was wrong - it becomes
        the new core's satellite and its satellites move under the new one
        as history); a verbatim core; or an age_out (the short-term is let
        go). Denying records the critique; the hippocampus redrafts that
        operation and resubmits.
        Critiques should be specific - the next attempt is written from
        them. An operation with target_op attaches to the new core another
        operation in this draft creates: approve that one too (it applies
        first), or deny this one.

        AN ATTACH WITH restated_content REPLACES THE CORE'S WORDING. Add
        "restate": false to attach the episode and leave the core as it is
        (the usual right answer), or "restate": true once you have compared
        the rewording with target_core in get_draft. With neither, a
        rewording that would wipe the core is refused.

        THE LAST ATTEMPT (terminal: true) is where you land things, not
        where you deny them: a denial there drops the operation. Take the
        best of the attempts, fix what is wrong, approve:
          {"op": 0, "verdict": "approve", "edited_content": "the text as it should read"}
          {"op": 1, "verdict": "approve", "edited_kind": "attach", "edited_target_core_id": "<core id>"}
        edited_kind is new_core, attach, supersede, verbatim or age_out. These
        edits are refused on any earlier attempt: there, deny and let the loop
        do its job. Deny on the last attempt only what should not be in memory
        at all. A memory at the end of its week ages out at the tidy whatever
        happens, so denying its age_out does not keep it: to keep it, approve
        with edited_kind - attach + edited_target_core_id keeps the memory
        itself under that core.

        A SATELLITE CAN HANG ON MORE THAN ONE CORE. On an attach, on any
        attempt, name the others the episode also supports:
          {"op": 3, "verdict": "approve", "edited_also_core_ids": ["<core id>"]}
        And the other way: where a core is new or newly worded, get_draft
        shows nearby_satellites, existing episodes elsewhere that sit close
        to it. If one is part of this story too, on any attempt:
          {"op": 0, "verdict": "approve", "link_satellite_ids": ["<satellite id>"]}

        WHAT IT CONSUMES, on any attempt. Approving archives the short-terms
        the operation names in source_short_ids, and only those. Where
        get_draft shows sources_check, the operation leaves fragments in
        short-term (left_in_short_term, in full), and a later sleep drafts
        them again. If the approved text carries one - every sentence of it -
        name it, with the rest:
          {"op": 2, "verdict": "approve", "edited_source_short_ids": ["<id>", "<id>"]}
        If it does not, leave it: it waits for a later sleep, or ages out.

        note is about the review as a whole and is kept once, on the draft
        (extra.review_note); a note about one operation goes in its decision.
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

    def undo_restate(self, core_id: str, reason: str) -> dict:
        """FLAG a core to get its earlier wording back, after an approved
        attach replaced it with a rewording that turned out wrong (get_memory
        shows the earlier wording as metadata.restated_from).

        A flag, not a scalpel - the same shape as forget_memory. Nothing
        changes now: the hippocampus executes the flag on its next tick, and
        the replaced wording, your reason and the time stay on the core. It
        can only return a core to words a review already approved; it is not
        a way to edit long-term. A reason is required.
        """
        from ..draft import DraftError
        try:
            out = self.store.flag_undo_restate(core_id, reason)
        except KeyError:
            return {"ok": False, "error": f"no long-term entry '{core_id}'"}
        except DraftError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out, "note": "Flagged. The hippocampus puts the earlier wording back on its next tick."}

    def get_satellites(self, core_id: str) -> dict:
        """The surroundings of a core: its supporting episodes with their
        dates (shared ones included), and the core it replaced - which is
        among the satellites now (was_core), with that core's own episodes
        moved under this one (moved_from). The draft around a hit."""
        core = self.store.get_by_id(core_id)
        if core is None or core.get("tier") != "long":
            return {"ok": False, "error": f"no long-term entry '{core_id}'"}
        sats = self.store.satellites_of(core_id)
        sup = core["metadata"].get("supersedes")
        return {"core": core, "satellites": sats, "count": len(sats),
                "supersedes": self.store.get_by_id(sup) if sup else None}

    def where_else(self, memory_id: str, n: int = 5) -> dict:
        """Where else a long-term memory's story might hang. Changes nothing.

        For a CORE: the satellites under other cores that sit closest to it,
        each with the cores it hangs on now - an episode that is part of this
        story too can be linked here (link_satellite).

        For a SATELLITE: how close it sits to each core it hangs on now
        (hangs_on, with similarity), and the nearest cores it does not
        (nearest_other_cores). A misfile reads as its own core far below a
        stranger; a real two-core story as two cores both close.

        Similarity is a hint, not a verdict: in a store about one person,
        everything sits near everything else. Read the memories.
        """
        try:
            return {"ok": True, **self.store.where_else(memory_id, n=n)}
        except KeyError:
            return {"ok": False, "error": f"no long-term entry '{memory_id}'"}

    def link_satellite(self, satellite_id: str, core_id: str, reason: str) -> dict:
        """FLAG a satellite to hang on one more core: an episode that is part
        of that core's story too. It keeps the cores it hangs on now, and the
        new core gains it as evidence.

        A flag, the same gate as undo_restate: nothing changes now, the
        hippocampus executes it on its next tick, and the reason stays on the
        satellite (relinks). To MOVE a misfiled satellite, link it to its
        right core and unlink it from the wrong one; both wait for the same
        tick. A reason is required.
        """
        return self._relink(satellite_id, reason, link=[core_id])

    def unlink_satellite(self, satellite_id: str, core_id: str, reason: str) -> dict:
        """FLAG a satellite to come off one of its cores (a misfile, or a
        link that turned out wrong); that core gives back the evidence.

        A satellite always hangs somewhere: unlinking its only core is
        refused unless a link to another core is already flagged. Executed on
        the hippocampus's next tick, like link_satellite. A reason is required.
        """
        return self._relink(satellite_id, reason, unlink=[core_id])

    def fold_satellite(self, satellite_id: str, into_id: str, reason: str) -> dict:
        """FLAG a satellite that holds the same episode as another to fold
        into it: the episode kept twice (the same words under two cores, or
        twice under one) becomes one. where_else shows look_alikes, the
        satellites that sit near-identical to one - candidates, not verdicts.

        On the hippocampus's next tick: into_id hangs on every core either
        hung on, a core both stood under gives back the evidence it counted
        twice, the pictures come along, and the folded one's words (when they
        differ), date and the reason stay in into_id's folds. Then
        satellite_id goes. A core's earlier wording (was_core) is history
        and is not folded away. A reason is required.
        """
        from ..draft import DraftError
        try:
            out = self.store.flag_fold(satellite_id, into_id, reason)
        except KeyError:
            return {"ok": False, "error": f"no long-term entry '{satellite_id}'"}
        except DraftError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out, "note": "Flagged. The hippocampus folds it on its next tick."}

    def _relink(self, satellite_id: str, reason: str, **change: list[str]) -> dict:
        from ..draft import DraftError
        try:
            out = self.store.flag_relink(satellite_id, reason=reason, **change)
        except KeyError:
            return {"ok": False, "error": f"no long-term entry '{satellite_id}'"}
        except DraftError as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, **out, "note": "Flagged. The hippocampus moves it on its next tick."}

def _pruned_because(meta: dict[str, Any]) -> str:
    """What sent a pruned entry to the archive, in a sentence."""
    if meta.get("retracted"):
        return f"retracted: {meta['retracted']}"
    if meta.get("consumed_by_draft"):
        kind = meta.get("consumed_as")
        if kind == "age_out":
            return (f"aged out by an approved verdict (draft {meta['consumed_by_draft']}, op "
                    f"{meta.get('consumed_by_op')}): let go on purpose")
        sats = meta.get("consumed_satellites") or []
        if isinstance(sats, str):
            sats = [sats] if not sats.startswith("[") else json.loads(sats)
        return (f"consumed, not lost: draft {meta['consumed_by_draft']} op {meta.get('consumed_by_op')} ({kind}) "
                f"made it part of {meta.get('consumed_into') or 'long-term'}" +
                (f"; its words live on as satellite {', '.join(sats)}" if sats else ""))
    if meta.get("aged_out"):
        return "aged out at the end of its life, without a verdict"
    return "archived before 23 Oct 2026, when the reason started being kept: check the drafts that cite it"


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
    mcp.tool()(impl.keep_with_memory)
    mcp.tool()(impl.look_at_memory)
    mcp.tool()(impl.drop_from_memory)

    # Open loops
    mcp.tool()(impl.remember_for_later)
    mcp.tool()(impl.complete_intent)

    # Agency surface
    mcp.tool()(impl.preserve_memory_verbatim)
    mcp.tool()(impl.release_memory_verbatim)
    mcp.tool()(impl.promote_memory_now)
    mcp.tool()(impl.forget_memory)
    mcp.tool()(impl.retract_memory)

    # The sleep: the brief that opens it, the drafts it proposes
    mcp.tool()(impl.submit_brief)
    # Drafts + the surroundings + the emergency door
    mcp.tool()(impl.list_drafts)
    mcp.tool()(impl.get_draft)
    mcp.tool()(impl.review_draft)
    mcp.tool()(impl.get_satellites)
    mcp.tool()(impl.undo_restate)
    mcp.tool()(impl.where_else)
    mcp.tool()(impl.link_satellite)
    mcp.tool()(impl.unlink_satellite)
    mcp.tool()(impl.fold_satellite)
    mcp.tool()(impl.audit_drafts)
    mcp.tool()(impl.purge_memory_now)

    return impl
