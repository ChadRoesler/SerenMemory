"""
seren_memory.collections
════════════════════════════════════════════════════════════════════════

The chroma abstraction. Wraps a single PersistentClient and exposes the
three tiers (+ briefs + pruned) as named collections. All the "talk to
chroma" logic lives here so routes and the consolidator never touch the
raw client.

WHY ONE CLIENT, MANY COLLECTIONS:
    Chroma's PersistentClient holds one on-disk store. Collections are
    cheap logical partitions inside it. Three collections = three tiers,
    one store, one client, one process. This is the whole reason
    SerenMemory bundles its own chroma instead of shelling out: direct
    in-process access, no subprocess dance, no sqlite shim gymnastics.

METADATA FLATTENING:
    Chroma metadata values must be str/int/float/bool - no nested dicts or
    lists. Our Pydantic models have an `extra: dict` and some have list
    fields. We flatten on write (prefix nested keys, JSON-encode lists) and
    unflatten on read. The flattening rules live here so they're consistent.
"""
from __future__ import annotations

import json
import time
from typing import Any, Optional

import chromadb
from chromadb.config import Settings

from .config import MemoryConfig
from .models.schemas import (
    LongTermEntry,
    NearTermEntry,
    ShortTermEntry,
    DailyBrief,
    Source,
    ConsolidatorRun,
    DraftEntry,
    DraftStatus,
)

# Chroma metadata can't hold None. We drop None-valued keys on write and
# treat their absence as None on read. This sentinel marks "this key was
# explicitly empty string" vs "this key was absent" if we ever need the
# distinction (we mostly don't).
def _clean_meta(d: dict[str, Any]) -> dict[str, Any]:
    """Flatten + sanitize a metadata dict for chroma.

    - Drops None values (chroma rejects them)
    - JSON-encodes list/dict values (chroma only takes scalars)
    - Leaves str/int/float/bool as-is
    """
    out: dict[str, Any] = {}
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
        elif isinstance(v, (list, dict)):
            out[k] = json.dumps(v)
        else:
            out[k] = str(v)
    return out


def _maybe_json(v: Any) -> Any:
    """Reverse of the list/dict encoding in _clean_meta. If a string looks
    like JSON, decode it; otherwise return as-is. Best-effort - a plain
    string that happens to start with [ or { is rare in our data."""
    if isinstance(v, str) and v[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


class MemoryStore:
    """Owns the chroma client and the tier collections."""

    def __init__(self, config: MemoryConfig, embedding_function: Any = None,
                 _allow_reset: bool = False):
        self._config = config
        persist = str(config.resolved_persist_dir())

        # anonymized_telemetry=False: we're a local homelab tool, no phoning
        # home. allow_reset is False in production; tests pass True so the
        # client can be torn down cleanly without leaving SQLite handles open.
        self._allow_reset = _allow_reset
        self._client = chromadb.PersistentClient(
            path=persist,
            settings=Settings(anonymized_telemetry=False,
                              allow_reset=_allow_reset),
        )

        s = config.storage
        # Embedding function resolution:
        #   - explicit embedding_function arg wins (tests, custom embedders)
        #   - else chroma's default (all-MiniLM-L6-v2, downloaded on first use)
        # We pass it to every collection so they share one embedding space -
        # critical for the unified /search to compare across tiers meaningfully.
        ef_kwargs = {}
        if embedding_function is not None:
            ef_kwargs["embedding_function"] = embedding_function

        # get_or_create so first boot just works.
        self.short = self._client.get_or_create_collection(s.short_collection, **ef_kwargs)
        self.near = self._client.get_or_create_collection(s.near_collection, **ef_kwargs)
        self.long = self._client.get_or_create_collection(s.long_collection, **ef_kwargs)
        self.briefs = self._client.get_or_create_collection(s.brief_collection, **ef_kwargs)
        # Pruned safety net - aged-out short-term entries land here for a
        # configurable window before true deletion. Insurance against a
        # bad consolidation heuristic.
        self.pruned = self._client.get_or_create_collection("seren_pruned", **ef_kwargs)
        # Consolidator run history - one record per run_once() call (success,
        # error, or noop). Gives 'last_consolidation_at' a durable answer
        # and the Halls viewer enough data for an operational panel.
        self.runs = self._client.get_or_create_collection("seren_consolidator_runs", **ef_kwargs)
        # Consolidator drafts - model review queue. Cluster syntheses land
        # here awaiting model approval before committing to long-term.
        # Verbatim peel-off and direct-promote bypass this queue (they carry
        # explicit pre-approval signals). On approve: shorts archive to pruned,
        # draft becomes long-term. On reject: critique stored, redraft triggered.
        self.drafts = self._client.get_or_create_collection(s.draft_collection, **ef_kwargs)

    def close(self) -> None:
        """Release the ChromaDB client and all collection references. In tests
        (allow_reset=True) also resets the database so SQLite WAL files are
        fully flushed before the temp directory is removed. Safe to call more
        than once.
        """
        try:
            if self._allow_reset:
                self._client.reset()
        except Exception:  # noqa: BLE001
            pass
        # Drop collection refs so GC can collect the underlying objects.
        for attr in ("short", "near", "long", "briefs", "pruned", "runs", "drafts"):
            try:
                delattr(self, attr)
            except AttributeError:
                pass

    # ------------------------------------------------------------------
    #  ShortTerm
    # ------------------------------------------------------------------
    def add_short(self, entry: ShortTermEntry) -> ShortTermEntry:
        meta = _clean_meta({
            "topic": entry.topic,
            "source": entry.source.value,
            "ts": entry.ts,
            "pinned": entry.pinned,
            **entry.extra,
        })
        self.short.add(documents=[entry.content], metadatas=[meta], ids=[entry.id])
        return entry

    def get_short_all(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """All short-term entries as dicts. Used by the consolidator. No
        similarity search - just yank everything (cheap; chroma.get with no
        query is a disk read, not a vector op)."""
        res = self.short.get(include=["documents", "metadatas"])
        return _zip_get(res, limit)

    def delete_short(self, ids: list[str]) -> None:
        if ids:
            self.short.delete(ids=ids)
    
    def update_short_metadata(self, entry_id: str, updates: dict[str, Any]) -> bool:
        """Update metadata on a short-term entry. Returns True if found.

        Short-term is documented as 'free read/write' - this isn't the
        Lacuna boundary protecting long-term. Used by preserve_verbatim to
        flip the verbatim flag, and is a general-purpose seam if other
        lightweight short-term tweaks come up later.
        """
        existing = self.short.get(ids=[entry_id], include=["documents", "metadatas"])
        if not existing.get("ids"):
            return False
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        meta.update(_clean_meta(updates))
        self.short.update(ids=[entry_id], metadatas=[meta])
        return True

    def promote_short_to_long(self, entry_id: str) -> Optional[str]:
        """Move a short-term entry to long-term verbatim, immediately.

        Bypasses the consolidator's clustering/synthesis. Content copied
        AS-IS, source short-term entry removed. Returns the new long-term
        ID, or None if the source doesn't exist.

        This is the 'I know this is durable, don't make me wait for the
        dream cycle' escape hatch. Use sparingly - the consolidator's
        clustering is usually the right path; this is the override.
        """
        from .models.schemas import LongTermEntry, Source
        existing = self.short.get(ids=[entry_id], include=["documents", "metadatas"])
        if not existing.get("ids"):
            return None
        content = existing["documents"][0]
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        long_entry = LongTermEntry(
            content=content,
            topic=meta.get("topic"),
            evidence_count=1,
            source=Source.CONSOLIDATOR,
            extra={"promoted_directly": True, "original_short_id": entry_id},
        )
        self.add_long(long_entry)
        self.short.delete(ids=[entry_id])
        return long_entry.id

    # ------------------------------------------------------------------
    #  NearTerm
    # ------------------------------------------------------------------
    def add_near(self, entry: NearTermEntry) -> NearTermEntry:
        meta = _clean_meta({
            "topic": entry.topic,
            "source": entry.source.value,
            "trigger_type": entry.trigger_type.value,
            "trigger_value": entry.trigger_value,
            "created_at": entry.created_at,
            "expires_at": entry.expires_at,
            "completed": entry.completed,
            "completed_at": entry.completed_at,
            **entry.extra,
        })
        self.near.add(documents=[entry.intent], metadatas=[meta], ids=[entry.id])
        return entry

    def get_near_all(self) -> list[dict[str, Any]]:
        res = self.near.get(include=["documents", "metadatas"])
        return _zip_get(res, None)

    def update_near(self, entry_id: str, updates: dict[str, Any]) -> bool:
        """Update metadata fields on a near-term entry (e.g. mark completed).
        This is NOT a Lacuna-style surgical content edit - it's flipping a
        status flag on an entry the caller legitimately owns. Returns True
        if the entry existed."""
        existing = self.near.get(ids=[entry_id], include=["documents", "metadatas"])
        if not existing.get("ids"):
            return False
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        meta.update(_clean_meta(updates))
        # chroma update keeps the document, swaps metadata
        self.near.update(ids=[entry_id], metadatas=[meta])
        return True

    def delete_near(self, ids: list[str]) -> None:
        if ids:
            self.near.delete(ids=ids)

    # ------------------------------------------------------------------
    #  LongTerm - writes are consolidator-only by convention (the route
    #  layer enforces; this layer trusts its callers). Reads open.
    # ------------------------------------------------------------------
    def add_long(self, entry: LongTermEntry) -> LongTermEntry:
        meta = _clean_meta({
            "topic": entry.topic,
            "evidence_count": entry.evidence_count,
            "created_at": entry.created_at,
            "last_confirmed": entry.last_confirmed,
            "superseded_by": entry.superseded_by,
            "forget_flag": entry.forget_flag,
            "source": entry.source.value,
            **entry.extra,
        })
        self.long.add(documents=[entry.content], metadatas=[meta], ids=[entry.id])
        return entry

    def supersede_long(self, old_id: str, new_id: str) -> bool:
        """Mark old_id as superseded by new_id. The non-destructive update
        path: the old fact stays for history, recall just stops surfacing it
        by default."""
        existing = self.long.get(ids=[old_id], include=["metadatas"])
        if not existing.get("ids"):
            return False
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        meta["superseded_by"] = new_id
        self.long.update(ids=[old_id], metadatas=[meta])
        return True

    def flag_long_forget(self, entry_id: str, reason: str) -> bool:
        """Record a forget-flag on a long-term entry. Does NOT delete - the
        consolidator decides what to do (purge if PII, demote if disputed)
        on its next run. The flag is the user's voice; the action is the
        consolidator's judgment."""
        existing = self.long.get(ids=[entry_id], include=["metadatas"])
        if not existing.get("ids"):
            return False
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        meta["forget_flag"] = reason
        self.long.update(ids=[entry_id], metadatas=[meta])
        return True

    def get_long_all(self) -> list[dict[str, Any]]:
        res = self.long.get(include=["documents", "metadatas"])
        return _zip_get(res, None)

    # ------------------------------------------------------------------
    #  Briefs
    # ------------------------------------------------------------------
    def add_brief(self, brief: DailyBrief) -> DailyBrief:
        meta = _clean_meta({
            "completed_intents": brief.completed_intents,
            "promote_hints": brief.promote_hints,
            "noise_hints": brief.noise_hints,
            "created_at": brief.created_at,
        })
        self.briefs.add(documents=[brief.summary], metadatas=[meta], ids=[brief.id])
        return brief

    def get_latest_brief(self) -> Optional[dict[str, Any]]:
        rows = _zip_get(self.briefs.get(include=["documents", "metadatas"]), None)
        if not rows:
            return None
        # Most recent by created_at
        rows.sort(key=lambda r: r["metadata"].get("created_at", 0), reverse=True)
        return rows[0]

    def get_recent_briefs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent N briefs by created_at. For the Halls viewer's
        brief panel and any caller that wants to scan steering history."""
        rows = _zip_get(self.briefs.get(include=["documents", "metadatas"]), None)
        rows.sort(key=lambda r: r["metadata"].get("created_at", 0), reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------
    #  Pruned safety net
    # ------------------------------------------------------------------
    def archive_pruned(self, rows: list[dict[str, Any]]) -> None:
        """Copy aged-out short-term rows to the pruned collection before
        deleting from short-term. Insurance window configured by
        consolidator.pruned_safety_days."""
        if not rows:
            return
        docs = [r["content"] for r in rows]
        metas = [_clean_meta({**r["metadata"], "pruned_at": time.time()}) for r in rows]
        ids = [r["id"] for r in rows]
        self.pruned.add(documents=docs, metadatas=metas, ids=ids)

    def sweep_pruned(self, older_than_seconds: int) -> int:
        """True-delete pruned entries past the safety window. Returns count
        deleted."""
        rows = _zip_get(self.pruned.get(include=["metadatas"]), None)
        now = time.time()
        stale = [r["id"] for r in rows
                 if now - r["metadata"].get("pruned_at", now) > older_than_seconds]
        if stale:
            self.pruned.delete(ids=stale)
        return len(stale)

    # ------------------------------------------------------------------
    #  Consolidator run history
    # ------------------------------------------------------------------
    def add_run(self, run: "ConsolidatorRun") -> "ConsolidatorRun":
        """Record one consolidation pass. The document text is a short
        human-readable summary (good for the embedding + viewer); the full
        numbers live in metadata."""
        summary_text = (
            f"Consolidator run {run.status.value}: "
            f"promoted={run.promoted}, aged_out={run.aged_out}, "
            f"near_expired={run.near_expired}, "
            f"completed_promoted={run.near_completed_promoted}, "
            f"forget_handled={run.forget_flags_handled}, "
            f"pruned_swept={run.pruned_swept}, "
            f"drafted={run.drafted}, "
            f"duration={run.duration_seconds:.2f}s"
        )
        meta = _clean_meta({
            "started_at": run.started_at,
            "finished_at": run.finished_at,
            "duration_seconds": run.duration_seconds,
            "status": run.status.value,
            "promoted": run.promoted,
            "drafted": run.drafted,
            "aged_out": run.aged_out,
            "near_expired": run.near_expired,
            "near_completed_promoted": run.near_completed_promoted,
            "forget_flags_handled": run.forget_flags_handled,
            "pruned_swept": run.pruned_swept,
            "brief_id_used": run.brief_id_used,
            "brief_was_pulled": run.brief_was_pulled,
            "error": run.error,
            "counts_after": run.counts_after,
        })
        self.runs.add(documents=[summary_text], metadatas=[meta], ids=[run.id])
        return run

    def get_latest_run(self) -> Optional[dict[str, Any]]:
        """Most recent run by finished_at. None if the consolidator never ran."""
        rows = _zip_get(self.runs.get(include=["documents", "metadatas"]), None)
        if not rows:
            return None
        rows.sort(key=lambda r: r["metadata"].get("finished_at", 0), reverse=True)
        return rows[0]

    def get_recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Most recent N runs by finished_at. For the Halls viewer's run-history panel."""
        rows = _zip_get(self.runs.get(include=["documents", "metadatas"]), None)
        rows.sort(key=lambda r: r["metadata"].get("finished_at", 0), reverse=True)
        return rows[:limit]

    # ------------------------------------------------------------------
    #  Consolidator drafts - HITL gate between cluster synthesis and
    #  long-term commit. See DraftEntry docstring for the philosophy.
    # ------------------------------------------------------------------
    def add_draft(self, draft: DraftEntry) -> DraftEntry:
        """Stage a synthesized cluster as a draft. Source shorts stay in
        place - they're the evidence trail until the draft is approved
        (then archived to pruned) or rejected (then back in the pool)."""
        meta = _clean_meta({
            "topic": draft.topic,
            "evidence_count": draft.evidence_count,
            "source_short_ids": draft.source_short_ids,  # _clean_meta JSON-encodes lists
            "brief_id_used": draft.brief_id_used,
            "cluster_id": draft.cluster_id or draft.id,
            "attempt": draft.attempt,
            "previous_draft_ids": draft.previous_draft_ids,
            "created_at": draft.created_at,
            "status": draft.status.value,
            "reviewed_at": draft.reviewed_at,
            "critique": draft.critique,
            "long_term_id": draft.long_term_id,
            "source": draft.source.value,
            **draft.extra,
        })
        self.drafts.add(documents=[draft.content], metadatas=[meta], ids=[draft.id])
        return draft

    def _get_draft_row(self, draft_id: str) -> Optional[dict[str, Any]]:
        """Fetch one draft by id, or None. Returns the same dict shape as
        _zip_get rows: {id, content, metadata}."""
        res = self.drafts.get(ids=[draft_id], include=["documents", "metadatas"])
        rows = _zip_get(res, None)
        return rows[0] if rows else None

    def get_recent_drafts(self, limit: int = 20,
                          status: Optional[str] = None) -> list[dict[str, Any]]:
        """Most recent drafts by created_at, newest first. Optional status
        filter - pass 'pending' for the review queue, 'approved',
        'rejected', or 'requires_selection' for history."""
        rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        if status:
            rows = [r for r in rows if r["metadata"].get("status") == status]
        # Unflatten list fields back for callers
        for r in rows:
            for field in ("source_short_ids", "previous_draft_ids"):
                val = r["metadata"].get(field)
                if isinstance(val, str):
                    r["metadata"][field] = _maybe_json(val)
        rows.sort(key=lambda r: r["metadata"].get("created_at", 0), reverse=True)
        return rows[:limit]

    def get_drafts_by_cluster(self, cluster_id: str) -> list[dict[str, Any]]:
        """All drafts sharing a cluster_id, ordered by attempt ascending.
        Returns the full chain for a redraft sequence so the model can
        compare all attempts when requires_selection is reached."""
        rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        chain = [r for r in rows if r["metadata"].get("cluster_id") == cluster_id]
        for r in chain:
            for field in ("source_short_ids", "previous_draft_ids"):
                val = r["metadata"].get(field)
                if isinstance(val, str):
                    r["metadata"][field] = _maybe_json(val)
        chain.sort(key=lambda r: r["metadata"].get("attempt", 1))
        return chain

    def approve_draft(self, draft_id: str,
                      note: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Commit a pending draft to long-term. Source shorts are archived
        to the pruned tier and removed from short. The draft is marked
        APPROVED with a forward link to the new long-term entry's id.

        Returns {long_term_id, shorts_archived} on success, None if the
        draft doesn't exist or isn't pending.
        """
        draft_row = self._get_draft_row(draft_id)
        if not draft_row:
            return None
        if draft_row["metadata"].get("status") != DraftStatus.PENDING.value:
            return None  # already reviewed; idempotency over re-doing

        # 1. Build the long-term entry from the draft and commit it.
        long_entry = LongTermEntry(
            content=draft_row["content"],
            topic=draft_row["metadata"].get("topic"),
            evidence_count=int(draft_row["metadata"].get("evidence_count", 1) or 1),
            source=Source.CONSOLIDATOR,
            extra={"from_draft_id": draft_id,
                   "cluster_id": draft_row["metadata"].get("cluster_id", draft_id)},
        )
        self.add_long(long_entry)

        # 2. Archive source shorts to pruned, then remove from short.
        source_ids = draft_row["metadata"].get("source_short_ids", [])
        if isinstance(source_ids, str):
            source_ids = _maybe_json(source_ids) or []
        if not isinstance(source_ids, list):
            source_ids = []
        shorts_archived = 0
        if source_ids:
            existing = self.short.get(ids=source_ids, include=["documents", "metadatas"])
            rows = _zip_get(existing, None)
            if rows:
                self.archive_pruned(rows)
                self.delete_short([r["id"] for r in rows])
                shorts_archived = len(rows)

        # 3. Mark the draft itself as approved with a forward link.
        self.drafts.update(
            ids=[draft_id],
            metadatas=[_clean_meta({
                **draft_row["metadata"],
                "status": DraftStatus.APPROVED.value,
                "reviewed_at": time.time(),
                "review_note": note,
                "long_term_id": long_entry.id,
            })],
        )
        return {"long_term_id": long_entry.id, "shorts_archived": shorts_archived}

    def reject_draft(self, draft_id: str, critique: str) -> Optional[dict[str, Any]]:
        """Mark a draft as rejected with the model's critique. Source shorts
        stay in place (they'll re-cluster or be used for a redraft). Returns
        a dict with cluster metadata the caller needs to decide whether to
        redraft, or None if the draft was missing or already reviewed.

        Returned dict keys: cluster_id, attempt, source_short_ids,
        brief_id_used, topic, evidence_count.
        """
        draft_row = self._get_draft_row(draft_id)
        if not draft_row:
            return None
        if draft_row["metadata"].get("status") != DraftStatus.PENDING.value:
            return None
        self.drafts.update(
            ids=[draft_id],
            metadatas=[_clean_meta({
                **draft_row["metadata"],
                "status": DraftStatus.REJECTED.value,
                "reviewed_at": time.time(),
                "critique": critique,
            })],
        )
        meta = draft_row["metadata"]
        source_ids = meta.get("source_short_ids", [])
        if isinstance(source_ids, str):
            source_ids = _maybe_json(source_ids) or []
        return {
            "cluster_id": meta.get("cluster_id", draft_id),
            "attempt": int(meta.get("attempt", 1)),
            "source_short_ids": source_ids if isinstance(source_ids, list) else [],
            "brief_id_used": meta.get("brief_id_used"),
            "topic": meta.get("topic"),
            "evidence_count": int(meta.get("evidence_count", 1) or 1),
        }

    def mark_chain_requires_selection(self, cluster_id: str) -> None:
        """Flip all non-terminal drafts in a chain to requires_selection status.
        Called when the redraft attempt limit is reached. Both pending and
        rejected drafts are flipped so the model can compare every attempt
        and commit the best one via /drafts/{id}/select.
        """
        selectable = {DraftStatus.PENDING.value, DraftStatus.REJECTED.value}
        rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        for r in rows:
            if (r["metadata"].get("cluster_id") == cluster_id
                    and r["metadata"].get("status") in selectable):
                self.drafts.update(
                    ids=[r["id"]],
                    metadatas=[_clean_meta({
                        **r["metadata"],
                        "status": DraftStatus.REQUIRES_SELECTION.value,
                    })],
                )

    def select_draft(self, draft_id: str,
                     note: Optional[str] = None,
                     edited_content: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Commit a requires_selection draft to long-term. Marks sibling
        drafts in the chain as rejected. Source shorts archived + removed.

        If edited_content is provided (non-None), the long-term entry uses
        the edited text. The original synthesis stays in the draft's content
        field for audit (and is also copied into the long-term entry's
        extra dict as original_draft_content). If None, the draft commits
        as-is.

        Returns {long_term_id, shorts_archived, edited, edit_delta_chars}
        on success, None if the draft doesn't exist or isn't in
        requires_selection status.
        """
        draft_row = self._get_draft_row(draft_id)
        if not draft_row:
            return None
        if draft_row["metadata"].get("status") != DraftStatus.REQUIRES_SELECTION.value:
            return None

        cluster_id = draft_row["metadata"].get("cluster_id", draft_id)

        # Determine commit content. Edited text takes precedence when set.
        # The original draft.content stays intact so we always have the
        # "what the consolidator originally synthesized" answer; the editor's
        # version is what lands in long-term.
        original_content = draft_row["content"]
        was_edited = edited_content is not None
        commit_content = edited_content if was_edited else original_content
        edit_delta = abs(len(commit_content) - len(original_content)) if was_edited else 0

        long_extra = {
            "from_draft_id": draft_id,
            "cluster_id": cluster_id,
            "selected_from_chain": True,
        }
        if was_edited:
            long_extra["edited_on_select"] = True
            long_extra["original_draft_content"] = original_content

        long_entry = LongTermEntry(
            content=commit_content,
            topic=draft_row["metadata"].get("topic"),
            evidence_count=int(draft_row["metadata"].get("evidence_count", 1) or 1),
            source=Source.CONSOLIDATOR,
            extra=long_extra,
        )
        self.add_long(long_entry)

        source_ids = draft_row["metadata"].get("source_short_ids", [])
        if isinstance(source_ids, str):
            source_ids = _maybe_json(source_ids) or []
        if not isinstance(source_ids, list):
            source_ids = []
        shorts_archived = 0
        if source_ids:
            existing = self.short.get(ids=source_ids, include=["documents", "metadatas"])
            rows = _zip_get(existing, None)
            if rows:
                self.archive_pruned(rows)
                self.delete_short([r["id"] for r in rows])
                shorts_archived = len(rows)

        # Mark the selected draft approved with forward link + edit audit.
        new_meta = {
            **draft_row["metadata"],
            "status": DraftStatus.APPROVED.value,
            "reviewed_at": time.time(),
            "review_note": note,
            "long_term_id": long_entry.id,
        }
        if was_edited:
            new_meta["edited_content"] = edited_content
            new_meta["edit_delta_chars"] = edit_delta
        self.drafts.update(
            ids=[draft_id],
            metadatas=[_clean_meta(new_meta)],
        )

        # Mark all other requires_selection siblings as rejected (chain settled).
        all_rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        for r in all_rows:
            if (r["id"] != draft_id
                    and r["metadata"].get("cluster_id") == cluster_id
                    and r["metadata"].get("status") == DraftStatus.REQUIRES_SELECTION.value):
                self.drafts.update(
                    ids=[r["id"]],
                    metadatas=[_clean_meta({
                        **r["metadata"],
                        "status": DraftStatus.REJECTED.value,
                        "reviewed_at": time.time(),
                        "critique": "not selected - sibling chosen",
                    })],
                )

        return {"long_term_id": long_entry.id, "shorts_archived": shorts_archived,
                "edited": was_edited, "edit_delta_chars": edit_delta}

    # ------------------------------------------------------------------
    #  Query - used by the unified search route
    # ------------------------------------------------------------------
    def query(self, collection_name: str, query_text: str, n: int) -> list[dict[str, Any]]:
        """Similarity search against one collection. Returns hits with
        distance. collection_name in {short, near, long}."""
        col = {"short": self.short, "near": self.near, "long": self.long}.get(collection_name)
        if col is None:
            raise ValueError(f"unknown collection '{collection_name}'")
        if col.count() == 0:
            return []
        res = col.query(
            query_texts=[query_text],
            n_results=min(n, col.count()),
            include=["documents", "metadatas", "distances"],
        )
        hits: list[dict[str, Any]] = []
        ids = res.get("ids", [[]])[0]
        docs = res.get("documents", [[]])[0]
        metas = res.get("metadatas", [[]])[0]
        dists = res.get("distances", [[]])[0]
        for i in range(len(ids)):
            meta = {k: _maybe_json(v) for k, v in (metas[i] or {}).items()}
            hits.append({
                "id": ids[i],
                "content": docs[i],
                "metadata": meta,
                "distance": dists[i],
            })
        return hits

    def counts(self) -> dict[str, int]:
        return {
            "short": self.short.count(),
            "near": self.near.count(),
            "long": self.long.count(),
            "briefs": self.briefs.count(),
            "drafts": self.drafts.count(),
            "pruned": self.pruned.count(),
            "runs": self.runs.count(),
        }


def _zip_get(res: dict[str, Any], limit: Optional[int]) -> list[dict[str, Any]]:
    """Turn a chroma .get() result into a list of {id, content, metadata}
    dicts, decoding any JSON-encoded metadata values back to lists/dicts."""
    ids = res.get("ids", []) or []
    docs = res.get("documents", []) or []
    metas = res.get("metadatas", []) or []
    rows: list[dict[str, Any]] = []
    for i in range(len(ids)):
        meta = {k: _maybe_json(v) for k, v in (metas[i] or {}).items()}
        rows.append({
            "id": ids[i],
            "content": docs[i] if i < len(docs) else "",
            "metadata": meta,
        })
        if limit and len(rows) >= limit:
            break
    return rows
