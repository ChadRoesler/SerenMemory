"""
Four things the store got wrong at the chroma boundary, each pinned against
the real chroma client (no mocks - the bugs were in what chroma actually does).

1. DISTANCE SPACE. Collections were created on chroma's L2 default while the
   brute-force fallback scored cosine; the same entry scored ~2x differently
   depending on whether the HNSW segment had flushed. New collections are
   cosine, and a legacy L2 collection is rebuilt onto cosine on boot with its
   vectors copied, not re-embedded.

2. CRASH-SAFE REBUILD. The old migration deleted a collection and re-added in
   batches; a kill mid-way left a fraction, and the next boot blessed the
   fraction as the whole. A rebuild now copies into <name>__migrating, then
   swaps. Whichever side a crash lands on, the next boot ends with the
   complete data under the real name.

3. DISTILLED RE-EMBED. Live writes embed _retrieval_text(content, topic), not
   the document; the migration re-embedded documents, so recall regressed
   after every model change. The migration embeds the same key a live write
   does.

4. METADATA REMOVAL. chroma's update() and upsert() merge, so a key could
   never be removed through this store. A None value in an update removes it.
"""
from __future__ import annotations

import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings

from seren_memory.config import MemoryConfig, StorageConfig
from seren_memory.collections import MemoryStore, _retrieval_text
from seren_memory.models.schemas import ShortTermEntry
from seren_memory import rebuild as R
from seren_memory.embedder import MigrationProgress, migrate_store, write_stamp, _backup_path

chromadb = pytest.importorskip("chromadb")


class _Len3(EmbeddingFunction):
    """[len(text), 1, 0] - length-sensitive, so the distilled key (topic
    prefixed) embeds differently from the bare document."""
    def __call__(self, input: Documents) -> Embeddings:
        return [[float(len(d)), 1.0, 0.0] for d in input]

    @staticmethod
    def name() -> str:
        return "len3"


class _Len5(EmbeddingFunction):
    def __call__(self, input: Documents) -> Embeddings:
        return [[float(len(d)), 2.0, 3.0, 4.0, 5.0] for d in input]

    @staticmethod
    def name() -> str:
        return "len5"


def _cfg(tmp_path) -> MemoryConfig:
    return MemoryConfig(storage=StorageConfig(persist_dir=str(tmp_path / "chroma")))


def _raw(tmp_path):
    # Same settings as the store under test: chroma caches one System per
    # path and refuses a second client on it with different settings.
    return chromadb.PersistentClient(path=str(tmp_path / "chroma"),
                                     settings=Settings(anonymized_telemetry=False,
                                                       allow_reset=True))


def _open(tmp_path) -> MemoryStore:
    return MemoryStore(_cfg(tmp_path), embedding_function=_Len3(), _allow_reset=True)


# ── 1. distance space ────────────────────────────────────────────────────────

def test_new_collections_are_cosine(tmp_path):
    s = _open(tmp_path)
    try:
        for col in (s.short, s.near, s.long, s.briefs, s.pruned, s.runs, s.drafts):
            assert R.space_of(col) == "cosine", col.name
    finally:
        s.close()


def test_legacy_l2_collection_is_rebuilt_onto_cosine_with_its_vectors(tmp_path):
    raw = _raw(tmp_path)
    legacy = raw.get_or_create_collection("seren_short", embedding_function=_Len3())  # L2 default
    legacy.add(ids=["a", "b"], documents=["hello", "a longer one"],
               metadatas=[{"topic": "t", "ts": 1.0}, {"topic": "u", "ts": 2.0}])
    assert R.space_of(legacy) == "l2"
    before = {i: [float(x) for x in e] for i, e in
              zip(*[legacy.get(include=["embeddings"])[k] for k in ("ids", "embeddings")])}
    del legacy, raw

    s = _open(tmp_path)
    try:
        assert R.space_of(s.short) == "cosine"
        got = s.short.get(include=["embeddings", "metadatas"])
        after = {i: [float(x) for x in e] for i, e in zip(got["ids"], got["embeddings"])}
        assert after == before, "vectors copied, not re-embedded"
        assert {m["topic"] for m in got["metadatas"]} == {"t", "u"}
        assert not [c.name for c in s._client.list_collections() if c.name.endswith(R.MIGRATING_SUFFIX)]
    finally:
        s.close()


def test_hot_path_and_fallback_agree_on_cosine(tmp_path):
    s = _open(tmp_path)
    try:
        s.add_short(ShortTermEntry(content="alpha beta", topic="x"))
        hot = s.query("short", "alpha beta", 1)
        brute = s._query_brute(s.short, "alpha beta", 1)
        assert hot and brute
        assert abs(hot[0]["distance"] - brute[0]["distance"]) < 1e-6
    finally:
        s.close()


# ── 2. crash-safe rebuild ────────────────────────────────────────────────────

def test_an_interrupted_swap_is_finished_on_boot(tmp_path):
    # the crash landed between "delete original" and "rename copy"
    raw = _raw(tmp_path)
    copy = raw.get_or_create_collection("seren_short" + R.MIGRATING_SUFFIX,
                                        metadata=dict(R.COLLECTION_METADATA),
                                        embedding_function=_Len3())
    copy.add(ids=["a", "b"], documents=["one", "two"], metadatas=[{"ts": 1.0}, {"ts": 2.0}])
    del copy, raw

    s = _open(tmp_path)
    try:
        assert s.short.count() == 2
        assert sorted(s.short.get()["ids"]) == ["a", "b"]
        names = {c.name for c in s._client.list_collections()}
        assert "seren_short" in names and not any(n.endswith(R.MIGRATING_SUFFIX) for n in names)
    finally:
        s.close()


def test_a_partial_copy_is_discarded_and_the_original_kept(tmp_path):
    # the crash landed mid-copy: the original is whole, the copy is short
    raw = _raw(tmp_path)
    orig = raw.get_or_create_collection("seren_short", metadata=dict(R.COLLECTION_METADATA),
                                        embedding_function=_Len3())
    orig.add(ids=["a", "b", "c"], documents=["one", "two", "three"],
             metadatas=[{"ts": 1.0}, {"ts": 2.0}, {"ts": 3.0}])
    copy = raw.get_or_create_collection("seren_short" + R.MIGRATING_SUFFIX,
                                        metadata=dict(R.COLLECTION_METADATA),
                                        embedding_function=_Len3())
    copy.add(ids=["a"], documents=["one"], metadatas=[{"ts": 1.0}])
    del orig, copy, raw

    s = _open(tmp_path)
    try:
        assert s.short.count() == 3
        assert not any(c.name.endswith(R.MIGRATING_SUFFIX) for c in s._client.list_collections())
    finally:
        s.close()


def test_rebuild_never_touches_the_original_until_the_copy_is_complete(tmp_path):
    raw = _raw(tmp_path)
    col = raw.get_or_create_collection("seren_long", embedding_function=_Len3())
    col.add(ids=[f"i{n}" for n in range(5)], documents=[f"doc {n}" for n in range(5)],
            metadatas=[{"ts": float(n)} for n in range(5)])

    seen: list[int] = []

    def _boom(n: int) -> None:
        seen.append(n)
        raise RuntimeError("simulated crash mid-copy")

    with pytest.raises(RuntimeError):
        R.rebuild_collection(raw, "seren_long", ef=_Len3(), reembed=True, on_progress=_boom)
    assert raw.get_collection("seren_long").count() == 5, "original intact"
    fixed = R.reconcile(raw)
    assert fixed == {"finished": [], "discarded": ["seren_long"]}
    assert raw.get_collection("seren_long").count() == 5


# ── 3. distilled re-embed on migration ───────────────────────────────────────

COLL_NAMES = {
    "short_collection": "seren_short", "near_collection": "seren_near",
    "long_collection": "seren_long", "brief_collection": "seren_briefs",
    "draft_collection": "seren_consolidator_drafts",
}


def test_migration_embeds_the_distilled_key_not_the_document(tmp_path, monkeypatch):
    import seren_memory.embedder as E
    monkeypatch.setattr(E, "resolve_embedding_function",
                        lambda m, device="cpu": {"A": _Len3(), "B": _Len5()}.get(m))
    live = tmp_path / "chroma"
    raw = chromadb.PersistentClient(path=str(live), settings=Settings(anonymized_telemetry=False))
    short = raw.get_or_create_collection("seren_short", embedding_function=_Len3())
    short.add(ids=["s1"], documents=["hello world"], metadatas=[{"topic": "greet", "ts": 1.0}])
    near = raw.get_or_create_collection("seren_near", embedding_function=_Len3())
    near.add(ids=["n1"], documents=["ask about the thing"], metadatas=[{"ts": 1.0}])
    write_stamp(live, "A")
    del short, near, raw

    prog = MigrationProgress()
    migrate_store(live, "A", "B", COLL_NAMES, prog)
    assert prog.state == "done", prog.error
    assert prog.done == prog.total == 2

    raw = chromadb.PersistentClient(path=str(live), settings=Settings(anonymized_telemetry=False))
    s_emb = raw.get_collection("seren_short").get(include=["embeddings"])["embeddings"][0]
    n_emb = raw.get_collection("seren_near").get(include=["embeddings"])["embeddings"][0]
    key = _retrieval_text("hello world", "greet")
    # float32 on disk, so approx
    assert [float(x) for x in s_emb] == pytest.approx([float(len(key)), 2.0, 3.0, 4.0, 5.0]), \
        "short is embedded on the distilled key, as a live write is"
    assert [float(x) for x in n_emb] == pytest.approx([float(len("ask about the thing")), 2.0, 3.0, 4.0, 5.0]), \
        "near embeds its document (nothing to distill)"
    for name in ("seren_short", "seren_near"):
        assert R.space_of(raw.get_collection(name)) == "cosine"


def test_backup_names_do_not_collide(tmp_path):
    live = tmp_path / "chroma"
    live.mkdir()
    first = _backup_path(live)
    first.mkdir()
    second = _backup_path(live)
    assert second != first and second.name.startswith(first.name)


# ── 4. metadata removal ──────────────────────────────────────────────────────

def test_a_none_update_removes_the_key_and_keeps_the_vector(tmp_path):
    s = _open(tmp_path)
    try:
        e = s.add_short(ShortTermEntry(content="remember the flag", topic="flags",
                                       extra={"tmpflag": "set"}))
        before = [float(x) for x in s.short.get(ids=[e.id], include=["embeddings"])["embeddings"][0]]
        assert s.get_by_id(e.id)["metadata"]["tmpflag"] == "set"

        assert s.update_short_metadata(e.id, {"tmpflag": None, "note": "kept"})
        meta = s.get_by_id(e.id)["metadata"]
        assert "tmpflag" not in meta and meta["note"] == "kept" and meta["topic"] == "flags"
        after = [float(x) for x in s.short.get(ids=[e.id], include=["embeddings"])["embeddings"][0]]
        assert after == before, "removal re-adds with the stored vector, no re-embed"
        assert s.short.count() == 1
        assert s.update_short_metadata("nope", {"x": None}) is False
    finally:
        s.close()


def test_a_plain_update_still_merges_in_one_call(tmp_path):
    s = _open(tmp_path)
    try:
        e = s.add_short(ShortTermEntry(content="merge me", topic="m"))
        assert s.update_short_metadata(e.id, {"pinned": True})
        meta = s.get_by_id(e.id)["metadata"]
        assert meta["pinned"] is True and meta["topic"] == "m"
    finally:
        s.close()
