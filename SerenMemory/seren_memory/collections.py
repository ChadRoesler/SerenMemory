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

METADATA REMOVAL:
    chroma's update() and upsert() both MERGE metadata (checked against
    1.5.9): a key you leave out survives, and None is rejected, so no key
    could ever be removed through this store. _replace_metadata does the
    only thing that works - delete + re-add with the stored vector - and the
    update_* seams treat a None value as "remove this key".

DISTANCE SPACE:
    Every collection is created on cosine (rebuild.COLLECTION_METADATA). A
    collection created before 2026-09-23 got chroma's L2 default while the
    brute-force fallback below scored cosine; the same entry scored ~2x
    differently depending on whether the HNSW segment had flushed. On boot a
    non-cosine collection is rebuilt onto cosine with its vectors copied.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Optional
from pathlib import Path

import chromadb
from chromadb.config import Settings

from .config import MemoryConfig
from .docket import DocketMixin
from .models.schemas import (
    DocketStatus,
    OpStatus,
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


def _split_topics(topic: Optional[str]) -> list[str]:
    """Split a comma-joined topic string into normalized lowercase tags.
    Topics are stored as ONE comma-joined metadata string ("a, b, c" - the
    `remember` tool takes comma-separated tags); this turns that back into the
    individual tags an exact match works on. Used by query_by_topic - the
    read-side use of the same tags the consolidator clusters on."""
    if not topic:
        return []
    return [t.strip().lower() for t in topic.split(",") if t.strip()]


# How many chars of a memory's content feed its RETRIEVAL KEY (the thing we
# embed, NOT the content we return). A long blob embeds to a muddy centroid
# vector that out-cosines sharp one-line facts on fact-queries; a topic-anchored
# head embeds sharp. Tune with brain_eval.py (BrainEval/) once the cluster's
# back — same "let the bytes pick the value" loop we used for fusion weights.
# <=0 disables (embed full content; legacy behavior). See BrainEval/distill-short-term.md.
_RETRIEVAL_KEY_MAX_CHARS = 512


def _retrieval_text(content: str, topic: Optional[str],
                    cap: int = _RETRIEVAL_KEY_MAX_CHARS) -> str:
    """The string we EMBED for a memory — deliberately NOT the content we
    return. documents=[full content] still holds the whole memory; only the
    MATCH key is distilled. Topic-prefixed so the vector anchors to what the
    memory is *about*, not just its opening words."""
    body = content if cap <= 0 else content[:cap]
    return f"{topic}. {body}" if topic else body


class MemoryStore(DocketMixin):
    """Owns the chroma client and the tier collections. The docket
    (submit / review / apply) is mixed in from seren_memory.docket."""

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
        #   - else the configured storage.embedding_model (resolved to a
        #     SentenceTransformer EF); None/"" -> chroma's default all-MiniLM.
        # We pass it to every collection so they share one embedding space -
        # critical for the unified /search to compare across tiers meaningfully.
        # NOTE: changing embedding_model on a store that already has data
        # silently corrupts recall (incompatible vector spaces). The startup
        # guard in __main__/app (check_store_state) catches that BEFORE we get
        # here; by the time MemoryStore is built, the model is either fresh,
        # matching, or post-migration - always consistent with the data.
        from .embedder import resolve_embedding_function, read_stamp, write_stamp
        ef_kwargs = {}
        if embedding_function is not None:
            ef_kwargs["embedding_function"] = embedding_function
        else:
            resolved_ef = resolve_embedding_function(s.embedding_model)
            if resolved_ef is not None:
                ef_kwargs["embedding_function"] = resolved_ef

        # A rebuild (migration or the cosine fix) a crash interrupted is
        # finished or discarded FIRST - get_or_create below would otherwise
        # stand an empty original beside a complete copy. See rebuild.py.
        from .rebuild import reconcile, space_of, rebuild_collection, COLLECTION_METADATA
        reconcile(self._client)

        def _open(name: str) -> Any:
            col = self._client.get_or_create_collection(
                name, metadata=dict(COLLECTION_METADATA), **ef_kwargs)
            if space_of(col) != "cosine":
                # Created before the space was pinned: same vectors, new
                # index. Copy, never re-embed - the model has not changed.
                rebuild_collection(self._client, name,
                                   ef=ef_kwargs.get("embedding_function"), reembed=False)
                col = self._client.get_or_create_collection(
                    name, metadata=dict(COLLECTION_METADATA), **ef_kwargs)
            return col

        # get_or_create so first boot just works.
        self.short = _open(s.short_collection)
        self.near = _open(s.near_collection)
        self.long = _open(s.long_collection)
        self.briefs = _open(s.brief_collection)
        # Pruned safety net - aged-out short-term entries land here for a
        # configurable window before true deletion. Insurance against a
        # bad consolidation heuristic.
        self.pruned = _open("seren_pruned")
        # Consolidator run history - one record per run_once() call (success,
        # error, or noop). Gives 'last_consolidation_at' a durable answer
        # and the Halls viewer enough data for an operational panel.
        self.runs = _open("seren_consolidator_runs")
        # Consolidator drafts - model review queue. Cluster syntheses land
        # here awaiting model approval before committing to long-term.
        # Verbatim peel-off and direct-promote bypass this queue (they carry
        # explicit pre-approval signals). On approve: shorts archive to pruned,
        # draft becomes long-term. On reject: critique stored, redraft triggered.
        self.drafts = _open(s.draft_collection)
        # Dockets - what the hippocampus proposes, reviewed per operation.
        self.dockets = _open("seren_dockets")

        # Stamp which embedder built this store (sidecar JSON in the persist
        # dir), so the next startup's guard can detect an embedder change. Only
        # write when we're NOT using a test-injected EF (those are ephemeral
        # and shouldn't claim the store was built with the configured model).
        # Idempotent: re-stamping with the same value each boot is harmless.
        if embedding_function is None:
            write_stamp(Path(persist), s.embedding_model)

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
        for attr in ("short", "near", "long", "briefs", "pruned", "runs", "drafts", "dockets"):
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
        # Embed a DISTILLED key, not the full blob (see _retrieval_text): the
        # full content still rides in documents[] so recall returns the whole
        # memory — only the MATCH vector is sharpened. If precompute fails for
        # any reason, degrade gracefully to chroma embedding the full content
        # (legacy behavior) so a hiccup never loses the write.
        try:
            key = _retrieval_text(entry.content, entry.topic)
            vec = self.short._embedding_function([key])[0]
            self.short.add(documents=[entry.content], embeddings=[vec],
                           metadatas=[meta], ids=[entry.id])
        except Exception:  # noqa: BLE001 - a precompute hiccup must not lose the write
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
        lightweight short-term tweaks come up later. A None value REMOVES
        that key (see _apply_metadata_updates).
        """
        return self._apply_metadata_updates(self.short, entry_id, updates)

    def _apply_metadata_updates(self, col: Any, entry_id: str,
                                updates: dict[str, Any]) -> bool:
        """Merge `updates` into an entry's metadata; a None value means
        "remove this key". Plain merges go through chroma's own update()
        (one call, atomic); a removal has to go through _replace_metadata,
        because chroma's update() and upsert() merge and cannot drop a key."""
        existing = col.get(ids=[entry_id], include=["metadatas"])
        if not existing.get("ids"):
            return False
        meta = dict(existing["metadatas"][0]) if existing.get("metadatas") else {}
        removals = [k for k, v in updates.items() if v is None]
        for k in removals:
            meta.pop(k, None)
        meta.update(_clean_meta(updates))
        if removals:
            return self._replace_metadata(col, entry_id, meta)
        col.update(ids=[entry_id], metadatas=[meta])
        return True

    def _replace_metadata(self, col: Any, entry_id: str, meta: dict[str, Any]) -> bool:
        """Set an entry's metadata to EXACTLY `meta`. chroma has no
        replace: update() and upsert() both merge (a key left out survives,
        None is rejected), so the only way to drop a key is delete + re-add.
        The stored vector rides along so nothing is re-embedded; the document
        is untouched. Not atomic - a crash between the two calls loses the
        entry - which is why the merge path is used whenever nothing needs
        removing."""
        got = col.get(ids=[entry_id], include=["documents", "metadatas", "embeddings"])
        if not got.get("ids"):
            return False
        embs = got.get("embeddings")
        vec = [float(x) for x in embs[0]] if embs is not None and len(embs) else None
        clean = _clean_meta(meta)
        col.delete(ids=[entry_id])
        kw: dict[str, Any] = {"ids": [entry_id], "documents": [got["documents"][0]]}
        if clean:
            kw["metadatas"] = [clean]
        if vec is not None:
            kw["embeddings"] = [vec]
        col.add(**kw)
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
        """Merge `updates` into a near-term entry's metadata. A None value
        removes that key (see _apply_metadata_updates)."""
        return self._apply_metadata_updates(self.near, entry_id, updates)

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
            "kind": entry.kind,
            "core_id": entry.core_id,
            "forget_flag": entry.forget_flag,
            "source": entry.source.value,
            **entry.extra,
        })
        # Distill the embed key, same as add_short (see _retrieval_text): embed
        # a topic-anchored head, keep documents=[full content] for return. A
        # long-term entry is a consolidated synthesis - already fairly tight -
        # but embedding the WHOLE synthesis still smears its match vector vs a
        # sharp query; the distilled key keeps it crisp and stops it out-
        # cosining one-line Loci facts on fact-queries. Degrade gracefully to
        # full-content embedding if precompute fails so a hiccup never loses
        # the write. (near-term is left full: intents are already one-liners,
        # there's nothing to distill.)
        try:
            key = _retrieval_text(entry.content, entry.topic)
            vec = self.long._embedding_function([key])[0]
            self.long.add(documents=[entry.content], embeddings=[vec],
                          metadatas=[meta], ids=[entry.id])
        except Exception:  # noqa: BLE001 - a precompute hiccup must not lose the write
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
    #  Cross-tier exact lookup - the dereference for a /search pointer
    # ------------------------------------------------------------------
    def get_by_id(self, entry_id: str) -> Optional[dict[str, Any]]:
        """Hydrate ONE entry by id across the recall tiers (short/near/long).

        The dereference path for a pointer handed back by /search: recall
        returns an id alongside each hit; this returns the WHOLE entry so a
        caller can inspect it closer or pull the context around it instead
        of re-searching. Returns {id, content, metadata, tier} or None if no
        recall tier holds that id. This is the right brain's twin of Loci's
        get_fact: exact lookup by handle, not ranked similarity search.

        chroma's .get(ids=[...]) returns an empty result (not an error) for a
        missing id, so a miss in one tier just falls through to the next.
        """
        for tier, col in (("short", self.short),
                          ("near", self.near),
                          ("long", self.long)):
            rows = _zip_get(
                col.get(ids=[entry_id], include=["documents", "metadatas"]), None)
            if rows:
                row = rows[0]
                row["tier"] = tier
                return row
        return None

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
        distance. collection_name in {short, near, long}.

        ChromaDB's HNSW index is written asynchronously — count() reads from
        SQLite (always current) but the .bin segment files may not exist yet
        for entries added in the current session. When that happens we fall
        back to a linear brute-force scan: fetch all docs via .get() (SQLite,
        always available) then re-embed the query and rank by cosine distance.
        For a personal memory store the counts are small enough this is fine.
        """
        col = {"short": self.short, "near": self.near, "long": self.long}.get(collection_name)
        if col is None:
            raise ValueError(f"unknown collection '{collection_name}'")
        if col.count() == 0:
            return []
        try:
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
        except Exception:  # noqa: BLE001
            # HNSW segment not flushed to disk yet — fall back to brute-force.
            return self._query_brute(col, query_text, n)

    def _query_brute(self, col: Any, query_text: str, n: int) -> list[dict[str, Any]]:
        """Linear cosine-similarity fallback used when the HNSW index isn't
        available (entries written but not yet persisted to disk). Re-embeds
        the query using the collection's own embedding function so the vector
        space is guaranteed to match. Cosine here and cosine on the
        collection (rebuild.COLLECTION_METADATA): the two paths score on one
        scale, which they did not while the collection defaulted to L2."""
        raw = col.get(include=["documents", "metadatas", "embeddings"])
        ids = raw.get("ids") or []
        docs = raw.get("documents") or []
        metas = raw.get("metadatas") or []
        # chroma 1.x hands embeddings back as a numpy array, whose truth value
        # is an error - `or []` here raised on every entry with a vector, so
        # the fallback this method exists for never ran (it 500'd instead).
        embeddings = raw.get("embeddings")
        if embeddings is None:
            embeddings = []
        if not ids or len(embeddings) == 0:
            return []

        # Re-embed the query through the same EF the collection uses.
        try:
            q_vecs = col._embedding_function([query_text])
            q_vec = q_vecs[0]
        except Exception:  # noqa: BLE001
            return []

        def _cosine_dist(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na = math.sqrt(sum(x * x for x in a))
            nb = math.sqrt(sum(x * x for x in b))
            if na == 0.0 or nb == 0.0:
                return 1.0
            return 1.0 - dot / (na * nb)

        scored: list[tuple[float, int]] = []
        for i, emb in enumerate(embeddings):
            if emb is not None:
                scored.append((_cosine_dist(q_vec, emb), i))
        scored.sort(key=lambda t: t[0])

        hits: list[dict[str, Any]] = []
        for dist, i in scored[:n]:
            meta = {k: _maybe_json(v) for k, v in (metas[i] or {}).items()}
            hits.append({
                "id": ids[i],
                "content": docs[i],
                "metadata": meta,
                "distance": dist,
            })
        return hits

    def query_by_topic(self, topics: list[str], n: int, *,
                       include_short: bool = True, include_near: bool = True,
                       include_long: bool = True, include_superseded: bool = False,
                       include_satellites: bool = False,
                       exclude_ids: Optional[list[str]] = None) -> list[dict[str, Any]]:
        """Retrieve entries TAGGED with any of `topics`, by exact tag match -
        the ASSOCIATION edge, not vector similarity. The read-side use of the
        topic tags the consolidator already clusters on: it surfaces an entry
        that shares a topic with what you're asking about even when its WORDING
        (e.g. a scar phrased in failure-language) put it far away in vector
        space - exactly the entry similarity search misses.

        Mechanism (deliberately NOT a vector query): chroma's metadata `where`
        can't match a single tag out of a comma-joined topic string, so this
        does the cheap thing the rest of this module already does - a .get()
        (pure SQLite, no embedder) per tier, then a Python tag-intersection.
        For a Nano-floor personal store the counts are small enough a linear
        scan is the right tool (same call shape as get_short_all and the
        brute-force query fallback).

        Ranking is by ASSOCIATION STRENGTH, not distance: more shared tags
        first (an entry tagged with two of your topics is a stronger edge than
        one sharing a single tag), then recency. There is no similarity score
        here by design - an edge earns its place by being tagged together, full
        stop. Each hit carries matched_topics + overlap so the caller (and the
        model) can see WHY it surfaced.

        exclude_ids drops entries the caller already has, so an edge join after
        a vector search returns only genuinely NEW context, not duplicates.
        Tier/superseded/completed filtering mirrors /search exactly.
        """
        wanted = {t.strip().lower() for t in topics if t and t.strip()}
        if not wanted:
            return []
        exclude = set(exclude_ids or ())
        tiers = []
        if include_short:
            tiers.append(("short", self.short))
        if include_near:
            tiers.append(("near", self.near))
        if include_long:
            tiers.append(("long", self.long))

        out: list[dict[str, Any]] = []
        for tier, col in tiers:
            for row in _zip_get(col.get(include=["documents", "metadatas"]), None):
                if row["id"] in exclude:
                    continue
                meta = row["metadata"]
                # Same tier filters as /search: skip superseded long-term
                # (unless asked) and completed near-term (history, not active).
                if tier == "long" and not include_satellites and meta.get("kind") == "satellite":
                    continue
                if tier == "long" and not include_superseded and meta.get("superseded_by"):
                    continue
                if tier == "near" and meta.get("completed"):
                    continue
                overlap = wanted & set(_split_topics(meta.get("topic")))
                if not overlap:
                    continue
                out.append({
                    "id": row["id"],
                    "content": row["content"],
                    "metadata": meta,
                    "tier": tier,
                    "matched_topics": sorted(overlap),
                    "overlap": len(overlap),
                })

        # Strongest association first (most shared tags), then most recent.
        def _recency(r: dict[str, Any]) -> float:
            m = r["metadata"]
            return float(m.get("ts") or m.get("created_at")
                         or m.get("last_confirmed") or 0)

        out.sort(key=lambda r: (r["overlap"], _recency(r)), reverse=True)
        return out[:n]

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

    # ------------------------------------------------------------------
    #  Maintenance - retroactive distill (re-embed onto the distilled key)
    # ------------------------------------------------------------------
    def reconcile_distill_keys(self, tiers: tuple[str, ...] = ("short", "long"),
                               dry_run: bool = True,
                               batch: int = 256) -> dict[str, Any]:
        """Re-embed existing entries onto the DISTILLED retrieval key, in place.

        The retroactive half of distill. add_short/add_long now embed a
        topic-anchored _retrieval_text key (not the full blob), but entries
        written before that landed still carry full-content vectors - which is
        exactly why old blobs out-cosine one-line facts on fact-queries. This
        recomputes each entry's vector from _retrieval_text(content, topic) and
        writes it back with collection.update(embeddings=...), which swaps ONLY
        the vector and leaves documents + metadata untouched.

        Why this is safe where migrate_store is heavy: the embedder MODEL isn't
        changing (same space, no delete/recreate, no dimension risk). It's
        lossless by construction (content/metadata never touched), idempotent
        (re-running yields the same vectors), and a precompute hiccup on a batch
        skips that batch rather than writing a bad vector.

        Only the searched-and-distilled tiers are touched: short and long. near
        is left alone (intents are one-liners; nothing to distill).

        dry_run=True (default) computes the would-be vectors and reports counts
        WITHOUT writing - run that first, eyeball it, then dry_run=False to
        apply. Take a persist_dir backup before applying; the op is
        non-destructive but a backup is the cheap insurance.
        """
        cols = {"short": self.short, "near": self.near, "long": self.long}
        report: dict[str, Any] = {"dry_run": dry_run, "tiers": {}}
        for tier in tiers:
            col = cols.get(tier)
            if col is None:
                report["tiers"][tier] = {"error": f"unknown/not-distilled tier '{tier}'"}
                continue
            got = col.get(include=["documents", "metadatas"])  # pure SQL, no EF
            ids = got.get("ids", []) or []
            docs = got.get("documents", []) or []
            metas = got.get("metadatas", []) or []
            reembedded = 0
            errors = 0
            for i in range(0, len(ids), batch):
                b_ids = ids[i:i + batch]
                b_docs = docs[i:i + batch]
                b_metas = metas[i:i + batch]
                keys = [_retrieval_text(d or "", (m or {}).get("topic"))
                        for d, m in zip(b_docs, b_metas)]
                try:
                    vecs = col._embedding_function(keys)
                    if not dry_run:
                        col.update(ids=b_ids, embeddings=vecs)
                    reembedded += len(b_ids)
                except Exception:  # noqa: BLE001 - never write a bad vector; skip + count
                    errors += len(b_ids)
            report["tiers"][tier] = {"entries": len(ids),
                                     "reembedded": reembedded,
                                     "errors": errors}
        return report


    # ------------------------------------------------------------------
    #  Purge - the forget flag, executed. A person's voice, with a tombstone
    #  and a cascade; the one true delete of long-term content.
    # ------------------------------------------------------------------
    def purge_long(self, entry_id: str, reason: str, *,
                   purge_backups: bool = True) -> Optional[dict[str, Any]]:
        """Remove a long-term entry and everything that still holds its
        content: its satellites (a core takes its surroundings with it), the
        source short-terms in the pruned tier and in short, drafts that became
        it, the docket operations that produced or touched it (scrubbed, not
        deleted - the verdicts are audit), and - by default - every migration
        backup beside the store, because a backup that keeps the leaked key
        is not a backup, it is the leak. A tombstone (id, kind, reason, time,
        what the cascade removed - never the content) goes to
        <persist_dir>/tombstones.jsonl. Returns the tombstone, or None if
        there was no such entry."""
        import json as _json
        import shutil
        row = self._long_row(entry_id)
        if row is None:
            return None
        meta = row["metadata"]
        kind = meta.get("kind", "core")
        ids = [entry_id]
        if kind == "core":
            ids += [s["id"] for s in self.satellites_of(entry_id)]

        # the short-terms these entries were made from
        sources: list[str] = []
        for i in ids:
            r = self._long_row(i)
            e = (r or {}).get("metadata", {})
            src = e.get("source_short_ids")
            if isinstance(src, str):
                src = _maybe_json(src)
            if isinstance(src, list):
                sources += [str(x) for x in src]
            if e.get("original_short_id"):
                sources.append(str(e["original_short_id"]))
        pruned_removed = shorts_removed = 0
        if sources:
            for col, counter in ((self.pruned, "pruned"), (self.short, "short")):
                present = list((col.get(ids=sources, include=[]) or {}).get("ids") or [])
                if present:
                    col.delete(ids=present)
                    if counter == "pruned":
                        pruned_removed = len(present)
                    else:
                        shorts_removed = len(present)

        # drafts that became one of these, or carry the same text
        contents = set()
        for i in ids:
            r = self._long_row(i)
            if r:
                contents.add(r["content"])
        drafts = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        dead = [d["id"] for d in drafts
                if d["metadata"].get("long_term_id") in ids or d["content"] in contents]
        if dead:
            self.drafts.delete(ids=dead)

        # dockets: scrub the operations, keep the verdicts
        scrubbed = 0
        for d in self.list_dockets(limit=100_000):
            changed = False
            for op in d.operations:
                if op.long_term_id in ids or op.target_core_id in ids or op.content in contents:
                    op.content = "[purged]"
                    op.edited_content = None
                    op.restated_content = None
                    op.rationale = None
                    changed = True
            if changed:
                d.summary = "[purged in part]"
                self._save_docket(d)
                scrubbed += 1

        self.long.delete(ids=ids)

        persist = self._config.resolved_persist_dir()
        backups = sorted(p for p in persist.parent.glob(persist.name + "_*") if p.is_dir())
        removed = 0
        if purge_backups:
            for b in backups:
                shutil.rmtree(b, ignore_errors=True)
                if not b.exists():
                    removed += 1
        tomb = {
            "id": entry_id, "kind": kind, "reason": reason, "purged_at": time.time(),
            "cascade": {"satellites": len(ids) - 1, "shorts": shorts_removed,
                        "pruned": pruned_removed, "drafts": len(dead),
                        "dockets_scrubbed": scrubbed, "backups_removed": removed,
                        "backups_retained": len(backups) - removed},
        }
        with (persist / "tombstones.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(_json.dumps(tomb) + "\n")
        return tomb

    def list_tombstones(self) -> list[dict[str, Any]]:
        import json as _json
        p = self._config.resolved_persist_dir() / "tombstones.jsonl"
        if not p.is_file():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(_json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
        return out

    def flagged_long(self) -> list[dict[str, Any]]:
        """Long-term entries carrying a forget flag - what the hippocampus
        purges at its next sleep."""
        return [r for r in self.get_long_all() if r["metadata"].get("forget_flag")]

    # ------------------------------------------------------------------
    #  Tidy - the mechanical steps of a sleep, owned by the store so the
    #  hippocampus (or the in-process consolidator) can ask for them over
    #  the API without holding a chroma client.
    # ------------------------------------------------------------------
    def held_short_ids(self) -> set[str]:
        """Short-terms a pending draft or a pending docket operation is built
        from. They are evidence under review: aging them out would leave a
        redraft with nothing to work from (the old consolidator did exactly
        that in the same run it drafted)."""
        held: set[str] = set()
        for d in self.get_recent_drafts(limit=100_000, status=DraftStatus.PENDING.value):
            src = d["metadata"].get("source_short_ids") or []
            held.update(str(x) for x in (src if isinstance(src, list) else []))
        for d in self.list_dockets(status=DocketStatus.PENDING.value, limit=100_000):
            for op in d.operations:
                if op.status == OpStatus.PENDING:
                    held.update(str(x) for x in op.source_short_ids)
        return held

    def age_out_short(self, cutoff_seconds: float, keep_pruned: bool = True) -> int:
        """Archive-then-delete short-term entries older than cutoff that are
        not pinned and not held by a pending draft or docket. Returns how many."""
        rows = self.get_short_all(limit=None)
        now = time.time()
        held = self.held_short_ids()
        stale = [r for r in rows
                 if not r["metadata"].get("pinned")
                 and r["id"] not in held
                 and (now - r["metadata"].get("ts", now)) > cutoff_seconds]
        if not stale:
            return 0
        if keep_pruned:
            self.archive_pruned(stale)
        self.delete_short([r["id"] for r in stale])
        return len(stale)

    def maintain_near(self) -> dict[str, int]:
        """Completed intents become a long-term record and leave near;
        expired ones are dropped."""
        rows = self.get_near_all()
        now = time.time()
        expired_ids: list[str] = []
        completed_ids: list[str] = []
        for r in rows:
            meta = r["metadata"]
            if meta.get("completed"):
                self.add_long(LongTermEntry(
                    content=f"Completed intent: {r['content']}",
                    topic="completed_intents", evidence_count=1,
                    source=Source.CONSOLIDATOR))
                completed_ids.append(r["id"])
                continue
            exp = meta.get("expires_at")
            if isinstance(exp, (int, float)) and exp > 0 and now > exp:
                expired_ids.append(r["id"])
        if completed_ids:
            self.delete_near(completed_ids)
        if expired_ids:
            self.delete_near(expired_ids)
        return {"expired": len(expired_ids), "completed_promoted": len(completed_ids)}


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
