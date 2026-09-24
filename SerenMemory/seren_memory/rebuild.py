"""
seren_memory.rebuild
════════════════════════════════════════════════════════════════════════

Rebuild a chroma collection in place, crash-safe, through chroma's own API.

Two callers need the same move:

  * the embedder migration (embedder.migrate_store): every entry re-embedded
    under a new model, and - because live writes embed a DISTILLED key
    (collections._retrieval_text), not the whole document - re-embedded on
    that same key, or recall regresses after every migration until someone
    finds /reconcile/distill;
  * the distance-space fix (collections.MemoryStore): a collection created
    before 2026-09-23 got chroma's L2 default while the brute-force fallback
    scored cosine, so the same entry scored ~2x differently depending on
    whether the HNSW segment had flushed. Those collections are rebuilt onto
    cosine on boot, copying the stored vectors as they are.

WHY A SWAP AND NOT DELETE-THEN-READD:
    The old migration deleted the collection and re-added in batches with the
    stamp written last. A kill mid-way left a collection holding a fraction
    of its rows, and the next boot's guard - stamp still old, so 'mismatch' -
    read that fraction as the whole store and re-migrated it as complete:
    256 of 300 survived and the store said done. Nothing here ever touches the
    original until a complete copy exists:

        1. copy every row into  <name>__migrating   (the original is untouched)
        2. delete the original
        3. rename the copy to <name>

    A crash anywhere leaves one of two shapes, and reconcile() on the next
    boot resolves both from the collection list alone, no marker file:

        original present  + __migrating present   -> the copy is partial (or
                                                    complete but unswapped):
                                                    drop the copy, keep the
                                                    original, redo later
        original missing  + __migrating present   -> the copy is complete
                                                    (step 2 ran): finish
                                                    step 3

    reconcile() has to run BEFORE anything get_or_creates the real names,
    or an empty original would appear beside a complete copy and read as
    the first shape. MemoryStore.__init__ and migrate_store both do that.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("seren_memory")

MIGRATING_SUFFIX = "__migrating"

# The distance space every collection is created with. Cosine matches what
# the brute-force fallback in collections._query_brute computes, so the hot
# path and the fallback score on one scale, and 1/(1+distance) in
# routes/search means the same thing whichever answered.
COLLECTION_METADATA = {"hnsw:space": "cosine"}

BATCH = 256


def space_of(col: Any) -> str:
    """The distance space a collection was created with ('cosine', 'l2',
    'ip'). chroma 1.x reports it in configuration_json; the legacy metadata
    key is checked too; absent both, chroma's default is l2."""
    cfg = getattr(col, "configuration_json", None)
    if isinstance(cfg, dict):
        hnsw = cfg.get("hnsw")
        if isinstance(hnsw, dict) and hnsw.get("space"):
            return str(hnsw["space"])
    meta = getattr(col, "metadata", None) or {}
    return str(meta.get("hnsw:space") or "l2")


def _as_list(vec: Any) -> list[float]:
    return [float(x) for x in vec]


def _add(dst: Any, ids: list[str], docs: list[Any], metas: list[Any],
         embs: Optional[list[list[float]]]) -> None:
    """One batch into the copy. chroma rejects an empty metadata dict, so
    rows with and without metadata go in as two calls."""
    with_meta = [i for i, m in enumerate(metas) if m]
    without = [i for i, m in enumerate(metas) if not m]
    for idx, use_meta in ((with_meta, True), (without, False)):
        if not idx:
            continue
        kw: dict[str, Any] = {
            "ids": [ids[i] for i in idx],
            "documents": [docs[i] for i in idx],
        }
        if use_meta:
            kw["metadatas"] = [metas[i] for i in idx]
        if embs is not None:
            kw["embeddings"] = [embs[i] for i in idx]
        dst.add(**kw)


def rebuild_collection(client: Any, name: str, *, ef: Any = None,
                       reembed: bool = False,
                       embed_for: Optional[Callable[[str, dict], str]] = None,
                       on_progress: Optional[Callable[[int], None]] = None) -> int:
    """Rebuild `name` as a cosine collection under `ef`, keeping ids,
    documents and metadata. Returns the row count.

    reembed=False  copy the stored vectors as they are (same model, new
                   space). Rows without a stored vector are re-embedded by
                   chroma from their document.
    reembed=True   throw the vectors away and embed again under `ef`; with
                   `embed_for(document, metadata) -> text`, embed THAT text
                   instead of the document (the distilled retrieval key).

    Crash-safe per the module docstring: the original is not touched until
    the copy is complete.
    """
    tmp = name + MIGRATING_SUFFIX
    src = client.get_collection(name)             # no EF: pure SQL reads
    try:
        client.delete_collection(tmp)             # a stale copy from a crash
    except Exception:  # noqa: BLE001
        pass
    kw = {"embedding_function": ef} if ef is not None else {}
    dst = client.get_or_create_collection(tmp, metadata=dict(COLLECTION_METADATA), **kw)

    include = ["documents", "metadatas"] + ([] if reembed else ["embeddings"])
    got = src.get(include=include)
    ids = list(got.get("ids") or [])
    docs = list(got.get("documents") or [])
    metas = [m or {} for m in (got.get("metadatas") or [])]
    if len(metas) < len(ids):
        metas += [{}] * (len(ids) - len(metas))
    embs = None if reembed else got.get("embeddings")
    have_embs = embs is not None and len(embs) == len(ids)
    # The function that embeds the distilled key: the configured one, or the
    # default chroma attached to the copy when nothing was configured.
    fn = ef if ef is not None else getattr(dst, "_embedding_function", None)

    for i in range(0, len(ids), BATCH):
        b_ids, b_docs, b_metas = ids[i:i + BATCH], docs[i:i + BATCH], metas[i:i + BATCH]
        if reembed and embed_for is not None and fn is not None:
            vecs = fn([embed_for(d or "", m) for d, m in zip(b_docs, b_metas)])
            _add(dst, b_ids, b_docs, b_metas, [_as_list(v) for v in vecs])
        elif not reembed and have_embs:
            _add(dst, b_ids, b_docs, b_metas, [_as_list(v) for v in embs[i:i + BATCH]])
        else:
            _add(dst, b_ids, b_docs, b_metas, None)   # chroma embeds the document
        if on_progress is not None:
            on_progress(len(b_ids))

    # The swap. Between these two calls the collection exists only as the
    # copy; reconcile() finishes the rename if a crash lands here.
    client.delete_collection(name)
    dst.modify(name=name)
    return len(ids)


def reconcile(client: Any) -> dict[str, list[str]]:
    """Finish or discard rebuilds a crash interrupted. Call before anything
    get_or_creates the real collection names."""
    names = {c.name for c in client.list_collections()}
    finished: list[str] = []
    discarded: list[str] = []
    for tmp in sorted(n for n in names if n.endswith(MIGRATING_SUFFIX)):
        orig = tmp[: -len(MIGRATING_SUFFIX)]
        if orig in names:
            client.delete_collection(tmp)
            discarded.append(orig)
        else:
            client.get_collection(tmp).modify(name=orig)
            finished.append(orig)
    if finished:
        log.warning("[seren-memory] finished an interrupted rebuild of %s", ", ".join(finished))
    if discarded:
        log.warning("[seren-memory] discarded a partial rebuild of %s (original kept)", ", ".join(discarded))
    return {"finished": finished, "discarded": discarded}
