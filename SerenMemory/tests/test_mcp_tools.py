"""Tests for MemoryToolImpl - the class behind every MCP tool.

These tests call the impl methods DIRECTLY, no FastMCP, no HTTP. The
structural split between MemoryToolImpl (the methods) and register_tools
(the FastMCP wiring) is what makes that possible. If a method's body
needs to change, change it once here; the FastMCP decoration in
register_tools picks up the new behaviour automatically.

The file is gated behind `pytest.importorskip("mcp")` at module load -
in environments where the [mcp] extras aren't installed (pure HTTP-only
deploys, CI without the optional dep), the whole file is skipped rather
than failing on import. Same pattern conftest.py uses for chromadb edge
cases.
"""
from __future__ import annotations

import pytest

# Guard the mcp-dependent import - tools.py imports from mcp.server.fastmcp
# at module top, so no SDK means no MemoryToolImpl. Use try/except +
# pytestmark so tests are still COLLECTED (visible in the test explorer)
# and shown as skipped rather than disappearing entirely.
try:
    import mcp  # noqa: F401
    from seren_memory.mcp.tools import MemoryToolImpl
    _mcp_available = True
except ImportError:
    _mcp_available = False
    MemoryToolImpl = None  # type: ignore

pytestmark = pytest.mark.skipif(
    not _mcp_available, reason="mcp extras not installed"
)

from seren_memory.collections import MemoryStore
from seren_memory.config import ConsolidatorConfig, MemoryConfig


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path, fake_embedder):
    """Fresh MemoryStore in a per-test tmp_path. Closed cleanly after."""
    cfg = MemoryConfig()
    cfg = cfg.model_copy(update={
        "storage": cfg.storage.model_copy(update={"persist_dir": str(tmp_path)})
    })
    s = MemoryStore(cfg, embedding_function=fake_embedder, _allow_reset=True)
    yield s
    s.close()


@pytest.fixture
def cfg(store):
    """The config that the store was built with - kept as a separate
    fixture so tests can pass it to MemoryToolImpl explicitly."""
    return store._config  # store retains its config


@pytest.fixture
def mcp_impl(store, cfg):
    """MemoryToolImpl over a real store. Nothing here drafts: that is the
    hippocampus's job."""
    return MemoryToolImpl(store, cfg)


# --- helper: seed shorts for tests that need them ---------------------------


def _seed_shorts(impl: MemoryToolImpl, *items) -> list[str]:
    """Write a batch of (content, topic) tuples to short, return ids."""
    return [impl.remember(content=c, topic=t)["id"] for c, t in items]


# ════════════════════════════════════════════════════════════════════════════
#  CORE MEMORY
# ════════════════════════════════════════════════════════════════════════════


def test_remember_writes_to_short(mcp_impl):
    """Basic write-then-read: remember() returns a short id, and the
    entry actually shows up in the underlying store."""
    r = mcp_impl.remember(content="user prefers Rust for systems work",
                          topic="preferences")
    assert r["ok"] is True
    assert r["tier"] == "short"
    assert r["id"]

    rows = mcp_impl.store.get_short_all(limit=None)
    assert any(row["content"] == "user prefers Rust for systems work"
               for row in rows)


def test_remember_accepts_optional_topic(mcp_impl):
    """topic is genuinely optional - no topic should still write OK
    (untagged entries go in the _untagged bucket during consolidation)."""
    r = mcp_impl.remember(content="just a note")
    assert r["ok"] is True


def test_recall_returns_ranked_hits(mcp_impl):
    """recall finds the entry we just wrote and returns it with a score."""
    mcp_impl.remember(content="favorite color is teal", topic="preferences")
    r = mcp_impl.recall(query="favorite color", n_results=5)
    assert r["query"] == "favorite color"
    assert any("teal" in h["content"] for h in r["hits"])
    # FakeEmbedder is hash-based, so we don't assert specific ranking,
    # only that we got something and the score is a number.
    assert all(isinstance(h["score"], (int, float)) for h in r["hits"])


def test_recall_respects_tier_flags(mcp_impl):
    """When include_short=False, short entries shouldn't appear in hits.
    Verifies the tier filter actually filters (the bug the plugin used
    to have where filters got silently dropped)."""
    mcp_impl.remember(content="i love teal", topic="preferences")
    r = mcp_impl.recall(query="teal", include_short=False,
                        include_near=False, include_long=False)
    assert r["hits"] == []
    assert r["searched_tiers"] == []


def test_what_do_you_remember_lists_short_entries(mcp_impl):
    """The inventory view (newest first)."""
    _seed_shorts(mcp_impl,
                 ("first thing", "t1"),
                 ("second thing", "t1"),
                 ("third thing", "t2"))
    r = mcp_impl.what_do_you_remember(limit=10)
    assert r["count"] == 3
    contents = [e["content"] for e in r["entries"]]
    assert "third thing" in contents


def test_what_do_you_remember_filters_by_topic(mcp_impl):
    """topic filter narrows to matching topic only."""
    _seed_shorts(mcp_impl,
                 ("alpha", "topicA"),
                 ("beta", "topicB"),
                 ("gamma", "topicA"))
    r = mcp_impl.what_do_you_remember(limit=10, topic="topicA")
    assert r["count"] == 2
    assert all(e["topic"] == "topicA" for e in r["entries"])


# ════════════════════════════════════════════════════════════════════════════
#  OPEN LOOPS (near-term)
# ════════════════════════════════════════════════════════════════════════════


def test_remember_for_later_writes_intent(mcp_impl):
    """Near-term: backend uses `intent`, not `content`. The impl bridges
    the naming gap; this test confirms it lands."""
    r = mcp_impl.remember_for_later(intent="bring up supersede-gap next time",
                                    topic="seren-followups")
    assert r["ok"] is True
    assert r["tier"] == "near"
    assert r["id"]


def test_remember_for_later_rejects_invalid_trigger_type(mcp_impl):
    """trigger_type is a closed enum - typos get a helpful error rather
    than a 500 from pydantic deep in the call chain."""
    r = mcp_impl.remember_for_later(intent="x", trigger_type="whenever_lol")
    assert r["ok"] is False
    assert "trigger_type" in r["error"]


def test_complete_intent_marks_completed(mcp_impl):
    """complete_intent flips the near-term entry's completed flag -
    consolidator promotes completed intents to long-term as a record."""
    written = mcp_impl.remember_for_later(intent="do thing", topic="t")
    intent_id = written["id"]
    r = mcp_impl.complete_intent(intent_id=intent_id)
    assert r["ok"] is True
    assert r["completed"] == intent_id


def test_complete_intent_missing_id_returns_error(mcp_impl):
    """Missing ID returns a structured error, not a 500."""
    r = mcp_impl.complete_intent(intent_id="not-a-real-id")
    assert r["ok"] is False
    assert "not-a-real-id" in r["error"]


# ════════════════════════════════════════════════════════════════════════════
#  AGENCY SURFACE
# ════════════════════════════════════════════════════════════════════════════


def test_preserve_memory_verbatim_flags_entry(mcp_impl):
    """The verbatim flag + pin land on the entry's metadata."""
    written = mcp_impl.remember(content="exact phrasing matters here",
                                topic="quotes")
    short_id = written["id"]
    r = mcp_impl.preserve_memory_verbatim(short_id=short_id)
    assert r["ok"] is True
    assert r["verbatim"] is True
    assert r["pinned"] is True


def test_preserve_memory_verbatim_missing_id_returns_error(mcp_impl):
    r = mcp_impl.preserve_memory_verbatim(short_id="missing")
    assert r["ok"] is False
    assert "missing" in r["error"]


def test_promote_memory_now_moves_to_long(mcp_impl):
    """The agent-side escape hatch - short entry goes to long verbatim,
    short entry is removed."""
    written = mcp_impl.remember(content="durable fact", topic="t")
    short_id = written["id"]
    r = mcp_impl.promote_memory_now(short_id=short_id)
    assert r["ok"] is True
    assert r["long_term_id"]
    assert r["removed_short_id"] == short_id

    # Verify it really left short.
    shorts = mcp_impl.store.get_short_all(limit=None)
    assert not any(row["id"] == short_id for row in shorts)


def test_promote_memory_now_missing_id_returns_error(mcp_impl):
    r = mcp_impl.promote_memory_now(short_id="missing")
    assert r["ok"] is False
    assert "missing" in r["error"]


def test_forget_memory_requires_reason(mcp_impl):
    """forget_memory rejects blank/whitespace reasons - the consolidator
    needs SOMETHING to steer with."""
    r = mcp_impl.forget_memory(long_id="any", reason="")
    assert r["ok"] is False
    assert "reason" in r["error"]

    r2 = mcp_impl.forget_memory(long_id="any", reason="   ")
    assert r2["ok"] is False


def test_forget_memory_missing_long_id_returns_error(mcp_impl):
    """Even with a valid reason, a non-existent long_id returns an error."""
    r = mcp_impl.forget_memory(long_id="not-real", reason="outdated info")
    assert r["ok"] is False
    assert "not-real" in r["error"]


# ════════════════════════════════════════════════════════════════════════════
#  BRIEF + CONSOLIDATION
# ════════════════════════════════════════════════════════════════════════════


def test_submit_brief_persists_with_hints(mcp_impl):
    """The brief and its hints actually land - submit_brief returns an
    id, and the brief is retrievable via the underlying store."""
    r = mcp_impl.submit_brief(
        summary="worked on memory autonomy + edit-on-select",
        promote_hints=["autonomy", "edit-on-select"],
        noise_hints=["typos"],
        completed_intents=["ask-about-supersede-gap"],
    )
    assert r["ok"] is True
    assert r["id"]

    briefs = mcp_impl.store.get_recent_briefs(limit=5)
    found = [b for b in briefs if b["id"] == r["id"]]
    assert len(found) == 1
    meta = found[0]["metadata"]
    assert "autonomy" in (meta.get("promote_hints") or [])
