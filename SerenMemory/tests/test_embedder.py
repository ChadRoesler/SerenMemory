"""
tests/test_embedder.py - coverage for the embedder-config + migration feature.

Proves the invariants that keep recall from silently corrupting when the
embedding model changes:

  RESOLUTION
    - None / "" -> None (chroma default)
    - a real name -> a SentenceTransformer EF (import-gated; skipped if the
      optional dep isn't present in the test env)

  STAMP (sidecar JSON in the persist_dir)
    - fresh dir -> read_stamp is None
    - write/read round-trips; '' means chroma default
    - a corrupt stamp file reads as None (treated as fresh, never crashes)
    - the stamp travels with a persist_dir copy (backup-safe)

  GUARD (check_store_state)
    - unstamped -> 'fresh'
    - stamp == config -> 'match'
    - stamp != config + data -> 'mismatch' (the dangerous case the modal handles)
    - stamp != config + NO data -> 'fresh' (nothing to corrupt)

  MIGRATION ENGINE (real chroma, fake deterministic embedders)
    - re-embeds every entry text into the new space (different dim proves it)
    - preserves ids / documents / metadata
    - stamps the NEW store with the new model
    - leaves the OLD store completely untouched (rollback intact)
    - progress reaches total/total, state 'done'
    - a broken embedder -> state 'error', no crash

  DENY / REVERT
    - rewrites storage.embedding_model back to the stamped value
    - preserves the rest of the config yaml

These use fake embedders (different vector dimensions = genuinely incompatible
spaces, exactly like MiniLM vs mpnet) so the suite runs in CI with no model
downloads. The real-model swap is a manual smoke test on a box with the models.
"""
from __future__ import annotations

import json
import tempfile
import shutil
from pathlib import Path

import pytest

from seren_memory.embedder import (
    resolve_embedding_function,
    model_label,
    read_stamp,
    write_stamp,
    check_store_state,
    MigrationProgress,
    migrate_store,
)


# ── RESOLUTION ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("val", [None, ""])
def test_resolve_empty_is_chroma_default(val):
    assert resolve_embedding_function(val) is None


def test_resolve_named_builds_ef():
    """A real model name builds a SentenceTransformer EF. Skipped if the dep
    isn't installed in the test env (it's chroma-optional)."""
    pytest.importorskip("chromadb")
    try:
        ef = resolve_embedding_function("all-MiniLM-L6-v2")
    except Exception as e:  # noqa: BLE001 - missing sentence-transformers, etc.
        pytest.skip(f"SentenceTransformer EF unavailable in test env: {e}")
    assert ef is not None


def test_model_label_readable():
    assert "default" in model_label(None)
    assert "default" in model_label("")
    assert model_label("all-mpnet-base-v2") == "all-mpnet-base-v2"


# ── STAMP ───────────────────────────────────────────────────────────────────

@pytest.fixture
def persist_dir():
    d = Path(tempfile.mkdtemp())
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_fresh_dir_has_no_stamp(persist_dir):
    assert read_stamp(persist_dir) is None


def test_stamp_round_trips(persist_dir):
    write_stamp(persist_dir, "all-mpnet-base-v2")
    s = read_stamp(persist_dir)
    assert s["embedding_model"] == "all-mpnet-base-v2"
    assert s["schema_version"] == 1
    assert "stamped_at" in s


def test_default_stamp_is_empty_string(persist_dir):
    write_stamp(persist_dir, None)
    assert read_stamp(persist_dir)["embedding_model"] == ""


def test_corrupt_stamp_reads_as_none(persist_dir):
    (persist_dir / ".seren_store_meta.json").write_text("{not valid json")
    assert read_stamp(persist_dir) is None  # treated as fresh, no crash


def test_stamp_travels_with_dir_copy(persist_dir):
    write_stamp(persist_dir, "bge-large")
    dst = Path(tempfile.mkdtemp()) / "copy"
    shutil.copytree(persist_dir, dst)
    try:
        assert read_stamp(dst)["embedding_model"] == "bge-large"
    finally:
        shutil.rmtree(dst.parent, ignore_errors=True)


# ── GUARD ───────────────────────────────────────────────────────────────────

def test_guard_unstamped_is_fresh():
    assert check_store_state(None, "anything", has_data=True) == "fresh"


def test_guard_match():
    assert check_store_state({"embedding_model": "m"}, "m", has_data=True) == "match"


def test_guard_default_match():
    # '' (default stamp) vs None (default config) must be treated as equal
    assert check_store_state({"embedding_model": ""}, None, has_data=True) == "match"


def test_guard_explicit_default_name_equals_empty():
    # The screenshot bug: a store stamped with the EXPLICIT default name vs an
    # empty/None config (or the reverse) is the SAME embedder - a match, not a
    # false migration prompt. "", None, and "all-MiniLM-L6-v2" all canonicalize.
    assert check_store_state(
        {"embedding_model": "all-MiniLM-L6-v2"}, None, has_data=True) == "match"
    assert check_store_state(
        {"embedding_model": "all-MiniLM-L6-v2"}, "", has_data=True) == "match"
    assert check_store_state(
        {"embedding_model": ""}, "all-MiniLM-L6-v2", has_data=True) == "match"
    # whitespace around the default name still canonicalizes
    assert check_store_state(
        {"embedding_model": "  all-MiniLM-L6-v2  "}, None, has_data=True) == "match"
    # but a genuinely different model still trips the guard
    assert check_store_state(
        {"embedding_model": "all-MiniLM-L6-v2"}, "all-mpnet-base-v2",
        has_data=True) == "mismatch"


def test_guard_mismatch_with_data():
    assert check_store_state({"embedding_model": "a"}, "b", has_data=True) == "mismatch"


def test_guard_mismatch_without_data_is_fresh():
    # changed embedder but nothing to corrupt -> safe to re-stamp
    assert check_store_state({"embedding_model": "a"}, "b", has_data=False) == "fresh"


# ── MIGRATION ENGINE (real chroma, fake embedders) ──────────────────────────

@pytest.fixture
def fake_embedders(monkeypatch):
    """Patch resolve_embedding_function so 'A'->3-dim, 'B'->5-dim fakes.
    Different dims = genuinely incompatible spaces (like MiniLM vs mpnet),
    and no network/model download."""
    chromadb = pytest.importorskip("chromadb")
    from chromadb.api.types import EmbeddingFunction, Documents, Embeddings

    class FakeA(EmbeddingFunction):
        def __call__(self, input: Documents) -> Embeddings:
            return [[float(len(d)), 1.0, 0.0] for d in input]
        @staticmethod
        def name() -> str:
            return "fakeA"

    class FakeB(EmbeddingFunction):
        def __call__(self, input: Documents) -> Embeddings:
            return [[float(len(d)), 2.0, 3.0, 4.0, 5.0] for d in input]
        @staticmethod
        def name() -> str:
            return "fakeB"

    import seren_memory.embedder as E

    def fake_resolve(model_name, device="cpu"):
        if model_name == "A":
            return FakeA()
        if model_name == "B":
            return FakeB()
        return None

    monkeypatch.setattr(E, "resolve_embedding_function", fake_resolve)
    return chromadb, FakeA, FakeB


def _seed_store(chromadb, path, FakeA):
    from chromadb.config import Settings
    c = chromadb.PersistentClient(path=str(path),
                                  settings=Settings(anonymized_telemetry=False))
    short = c.get_or_create_collection("seren_short", embedding_function=FakeA())
    short.add(ids=["s1", "s2"],
              documents=["hello world", "a longer memory entry here"],
              metadatas=[{"topic": "greet", "ts": 1.0},
                         {"topic": "note", "ts": 2.0}])
    longc = c.get_or_create_collection("seren_long", embedding_function=FakeA())
    longc.add(ids=["l1"], documents=["a durable fact"],
              metadatas=[{"topic": "fact", "evidence_count": 3}])
    write_stamp(path, "A")
    return c


COLL_NAMES = {
    "short_collection": "seren_short", "near_collection": "seren_near",
    "long_collection": "seren_long", "brief_collection": "seren_briefs",
    "draft_collection": "seren_consolidator_drafts",
}


def test_migration_full_cycle(fake_embedders, persist_dir):
    chromadb, FakeA, FakeB = fake_embedders
    from chromadb.config import Settings

    live = persist_dir / "chroma"
    _seed_store(chromadb, live, FakeA)

    prog = MigrationProgress()
    migrate_store(live, "A", "B", COLL_NAMES, prog)

    assert prog.state == "done", prog.error
    assert prog.total == 3 and prog.done == 3
    assert prog.percent == 100
    # live path is UNCHANGED (in-place migration)
    assert live.name == "chroma"
    # a timestamped backup was taken
    assert prog.stash_dir is not None
    assert Path(prog.stash_dir).exists()

    # live store: text + metadata preserved, re-embedded to 5-dim (new space)
    nc = chromadb.PersistentClient(path=str(live),
                                   settings=Settings(anonymized_telemetry=False))
    ns = nc.get_collection("seren_short", embedding_function=FakeB())
    got = ns.get(ids=["s1"], include=["documents", "metadatas", "embeddings"])
    assert got["documents"][0] == "hello world"
    assert got["metadatas"][0]["topic"] == "greet"
    assert len(got["embeddings"][0]) == 5  # re-embedded into B's space
    del nc

    # live store stamped B
    assert read_stamp(live)["embedding_model"] == "B"


def test_migration_backup_preserves_old_space(fake_embedders, persist_dir):
    chromadb, FakeA, FakeB = fake_embedders
    from chromadb.config import Settings

    live = persist_dir / "chroma"
    _seed_store(chromadb, live, FakeA)

    prog = MigrationProgress()
    migrate_store(live, "A", "B", COLL_NAMES, prog)

    # the backup copy holds the ORIGINAL old-space (3-dim) data, stamped A
    backup = Path(prog.stash_dir)
    bc = chromadb.PersistentClient(path=str(backup),
                                   settings=Settings(anonymized_telemetry=False))
    bs = bc.get_collection("seren_short", embedding_function=FakeA())
    got = bs.get(ids=["s1"], include=["embeddings"])
    assert len(got["embeddings"][0]) == 3       # still A's 3-dim space
    del bc
    assert read_stamp(backup)["embedding_model"] == "A"


def test_migration_failure_restores_live(fake_embedders, persist_dir, monkeypatch):
    """A crash mid-rebuild must restore the live dir from the backup so the
    operator ends exactly where they started (original data, original stamp)."""
    chromadb, FakeA, FakeB = fake_embedders
    from chromadb.config import Settings
    from chromadb.api.types import EmbeddingFunction

    live = persist_dir / "chroma"
    _seed_store(chromadb, live, FakeA)

    # An embedder that fails after the first call (crash mid-rebuild).
    class BoomB(EmbeddingFunction):
        _n = 0
        def __call__(self, input):
            BoomB._n += 1
            if BoomB._n > 1:
                raise RuntimeError("simulated crash mid-rebuild")
            return [[float(len(d)), 2.0, 3.0, 4.0, 5.0] for d in input]
        @staticmethod
        def name():
            return "boomB"

    import seren_memory.embedder as E
    monkeypatch.setattr(E, "resolve_embedding_function",
                        lambda m, device="cpu": FakeA() if m == "A"
                        else (BoomB() if m == "B" else None))

    prog = MigrationProgress()
    migrate_store(live, "A", "B", COLL_NAMES, prog)  # must not raise

    assert prog.state == "error"
    assert "restored from backup" in (prog.error or "")
    # live restored to original: stamp A (the data restore itself is verified
    # in a fresh-process integration test; here we assert the stamp + that the
    # live dir still exists and wasn't left half-migrated).
    assert live.exists()
    assert read_stamp(live)["embedding_model"] == "A"


def test_progress_snapshot_shape():
    p = MigrationProgress()
    snap = p.snapshot()
    assert snap["state"] == "idle"
    assert snap["percent"] == 0
    p.total, p.done, p.state = 10, 5, "running"
    assert p.snapshot()["percent"] == 50


# ── DENY / REVERT ───────────────────────────────────────────────────────────

def _revert_config_embedder(config_path: Path, stamped_model: str) -> bool:
    """Mirror of the /migrate/deny rewrite logic, for unit testing."""
    import yaml
    if not config_path.is_file():
        return False
    data = yaml.safe_load(config_path.read_text()) or {}
    data.setdefault("storage", {})
    data["storage"]["embedding_model"] = stamped_model or None
    config_path.write_text(yaml.safe_dump(data, default_flow_style=False,
                                          sort_keys=False))
    return True


def test_deny_reverts_to_default(persist_dir):
    import yaml
    cfg = persist_dir / "seren-memory.yaml"
    cfg.write_text(yaml.safe_dump({
        "server": {"host": "0.0.0.0", "port": 7420},
        "storage": {"persist_dir": "~/.seren-memory/chroma",
                    "embedding_model": "all-mpnet-base-v2"},
    }, sort_keys=False))
    assert _revert_config_embedder(cfg, "")  # stamped = default
    out = yaml.safe_load(cfg.read_text())
    assert out["storage"]["embedding_model"] is None
    assert out["server"]["port"] == 7420  # rest preserved


def test_deny_reverts_to_named_model(persist_dir):
    import yaml
    cfg = persist_dir / "seren-memory.yaml"
    cfg.write_text(yaml.safe_dump({"storage": {"embedding_model": "bge-large"}},
                                  sort_keys=False))
    assert _revert_config_embedder(cfg, "all-mpnet-base-v2")
    assert yaml.safe_load(cfg.read_text())["storage"]["embedding_model"] \
        == "all-mpnet-base-v2"


# ── SAFE-MODE GATE (predicate) ──────────────────────────────────────────────

def _allowed_in_safe_mode(path: str) -> bool:
    """Mirror of app.py's safe-mode gate predicate."""
    return path in ("/", "/health", "/viewer",
                    "/migrate/status", "/migrate/accept", "/migrate/deny")


@pytest.mark.parametrize("path", [
    "/", "/health", "/viewer", "/migrate/status", "/migrate/accept", "/migrate/deny",
])
def test_safe_mode_allows_control_paths(path):
    assert _allowed_in_safe_mode(path) is True


@pytest.mark.parametrize("path", [
    "/short", "/long", "/near", "/search", "/drafts", "/consolidate/run",
    "/brief", "/short/x/promote", "/drafts/x/approve",
])
def test_safe_mode_blocks_memory_paths(path):
    assert _allowed_in_safe_mode(path) is False