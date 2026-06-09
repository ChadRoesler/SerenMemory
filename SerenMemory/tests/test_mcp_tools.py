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
from seren_memory.consolidator.service import Consolidator


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
    """MemoryToolImpl with NO consolidator - the locked-down/no-LLM
    profile. Tools that need a consolidator (consolidate_now, reject
    redraft) return error responses in this profile, which is itself
    worth testing."""
    return MemoryToolImpl(store, cfg, consolidator=None)


@pytest.fixture
def mcp_impl_with_consolidator(store, cfg, monkeypatch):
    """MemoryToolImpl WITH a real Consolidator whose model call is
    stubbed - for testing reject's redraft path and consolidate_now's
    happy path without spinning up a real LLM."""
    cfg = cfg.model_copy(update={
        "consolidator": ConsolidatorConfig(enabled=False)  # don't autostart
    })
    consolidator = Consolidator(store, cfg)

    # Same stubbing pattern conftest.py recommends - bypass the LLM
    # call, return a canned synthesis instead.
    def _stub_call_model(self, prompt: str, **kwargs) -> str:
        return "STUB SYNTHESIS"
    monkeypatch.setattr(Consolidator, "_call_model", _stub_call_model)

    return MemoryToolImpl(store, cfg, consolidator=consolidator)


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


def test_consolidate_now_errors_when_consolidator_missing(mcp_impl):
    """The locked-down profile: no consolidator means consolidate_now
    returns a helpful error rather than crashing."""
    r = mcp_impl.consolidate_now()
    assert r["ok"] is False
    assert "consolidator not configured" in r["error"]


# ════════════════════════════════════════════════════════════════════════════
#  DRAFT REVIEW
# ════════════════════════════════════════════════════════════════════════════
#
# These tests need at least one draft to operate on. The cleanest way to
# get one without going through the full consolidator path is to call
# the store's add_draft directly with a constructed DraftEntry.


def _make_test_draft(store, **overrides):
    """Build a DraftEntry, write it to the store, return its id."""
    from seren_memory.models.schemas import DraftEntry, Source
    draft = DraftEntry(
        content="test draft content",
        topic="test-topic",
        evidence_count=2,
        source_short_ids=["short-1", "short-2"],
        source=Source.CONSOLIDATOR,
        attempt=1,
        **overrides,
    )
    draft.cluster_id = draft.id
    saved = store.add_draft(draft)
    return saved.id


def test_list_drafts_returns_entries(mcp_impl):
    """list_drafts surfaces pending entries."""
    _make_test_draft(mcp_impl.store)
    _make_test_draft(mcp_impl.store)
    r = mcp_impl.list_drafts(status="pending")
    assert r["count"] >= 2


def test_get_draft_chain_returns_attempts(mcp_impl):
    """get_draft_chain returns the chain for the draft's cluster_id."""
    draft_id = _make_test_draft(mcp_impl.store)
    r = mcp_impl.get_draft_chain(draft_id=draft_id)
    assert r["count"] >= 1
    assert any(a["id"] == draft_id for a in r["attempts"])


def test_get_draft_chain_missing_id_returns_error(mcp_impl):
    r = mcp_impl.get_draft_chain(draft_id="not-real")
    assert r["ok"] is False


def test_approve_draft_commits_to_long(mcp_impl):
    """approve_draft writes to long-term and reports the long_term_id."""
    draft_id = _make_test_draft(mcp_impl.store)
    r = mcp_impl.approve_draft(draft_id=draft_id, note="LGTM")
    assert r["ok"] is True
    assert r["long_term_id"]


def test_reject_draft_requires_critique(mcp_impl):
    """Critique is mandatory - blank/whitespace gets rejected."""
    draft_id = _make_test_draft(mcp_impl.store)
    r = mcp_impl.reject_draft(draft_id=draft_id, critique="")
    assert r["ok"] is False
    assert "critique" in r["error"]


def test_reject_draft_errors_when_consolidator_missing(mcp_impl):
    """No consolidator means we can't redraft - return the helpful error."""
    draft_id = _make_test_draft(mcp_impl.store)
    r = mcp_impl.reject_draft(draft_id=draft_id, critique="specific reason")
    assert r["ok"] is False
    assert "consolidator" in r["error"]


def test_reject_draft_triggers_redraft_with_consolidator(
        mcp_impl_with_consolidator):
    """With a (stubbed) consolidator, reject triggers a redraft and the
    new draft id appears in the response."""
    draft_id = _make_test_draft(mcp_impl_with_consolidator.store)
    r = mcp_impl_with_consolidator.reject_draft(
        draft_id=draft_id,
        critique="conflated supersede-gap with cluster threshold; separate them")
    assert r["ok"] is True
    # Redraft action either creates a new draft (action='redrafted') or
    # flips to requires_selection if the attempt cap is hit. Either way,
    # we should have a valid action recorded.
    assert r["action"] in ("redrafted", "requires_selection")


def test_select_draft_with_edited_content(mcp_impl):
    """select_draft with edited_content commits the edit to long-term
    instead of the original draft text. The draft row keeps the
    original synthesis for audit."""
    # Set up: create a draft in requires_selection state by manually
    # flipping its status (matches what would happen after redraft cap).
    from seren_memory.models.schemas import DraftStatus
    draft_id = _make_test_draft(mcp_impl.store)
    mcp_impl.store.mark_chain_requires_selection(draft_id)

    edited = "the editor's polished long-term statement"
    r = mcp_impl.select_draft(draft_id=draft_id, edited_content=edited)
    assert r["ok"] is True
    assert r["edited"] is True
    assert r["edit_delta_chars"] > 0

    # The long-term entry should have the edited text.
    long_rows = mcp_impl.store.get_long_all()
    long_entry = next(r for r in long_rows
                      if r["metadata"].get("from_draft_id") == draft_id)
    assert long_entry["content"] == edited


def test_select_draft_rejects_blank_edit(mcp_impl):
    """Empty/whitespace edited_content is rejected - it's a bug
    surface, not a sensible 'no edit'."""
    draft_id = _make_test_draft(mcp_impl.store)
    mcp_impl.store.mark_chain_requires_selection(draft_id)

    r1 = mcp_impl.select_draft(draft_id=draft_id, edited_content="")
    assert r1["ok"] is False

    r2 = mcp_impl.select_draft(draft_id=draft_id, edited_content="   \n  ")
    assert r2["ok"] is False


# ════════════════════════════════════════════════════════════════════════════
#  SELF-CONSOLIDATION (model-as-consolidator)
# ════════════════════════════════════════════════════════════════════════════


def test_prepare_consolidation_returns_clusters(mcp_impl):
    """prepare_consolidation groups shorts by topic and returns a prompt
    template. The CALLING model is expected to do the actual synthesis."""
    _seed_shorts(mcp_impl,
                 ("fact one", "topic-A"),
                 ("fact two", "topic-A"),
                 ("fact three", "topic-B"))
    r = mcp_impl.prepare_consolidation()
    assert r["ok"] is True
    assert "topic-A" in r["clusters"]
    assert "topic-B" in r["clusters"]
    assert len(r["clusters"]["topic-A"]) == 2
    assert r["prompt_template"]


def test_prepare_consolidation_empty_store_returns_note(mcp_impl):
    """Empty short-term: no clusters, just a friendly note."""
    r = mcp_impl.prepare_consolidation()
    assert r["ok"] is True
    assert r["clusters"] == {}
    assert "no short-term" in r["note"]


def test_commit_consolidation_creates_pending_draft(mcp_impl):
    """The model-synthesised draft lands as pending - not auto-committed
    to long-term (one self-review is too weak a gate)."""
    ids = _seed_shorts(mcp_impl,
                       ("fact one", "topic-A"),
                       ("fact two", "topic-A"))
    r = mcp_impl.commit_consolidation(
        draft_text="consolidated truth about topic-A",
        source_short_ids=ids,
        topic="topic-A",
    )
    assert r["ok"] is True
    assert r["status"] == "pending"
    assert r["draft_id"]

    # Verify the draft is actually queryable as pending.
    drafts = mcp_impl.list_drafts(status="pending")
    assert any(e["id"] == r["draft_id"] for e in drafts["entries"])


def test_commit_consolidation_rejects_blank_draft(mcp_impl):
    r = mcp_impl.commit_consolidation(
        draft_text="   ",
        source_short_ids=["x"],
    )
    assert r["ok"] is False
    assert "draft_text" in r["error"]


def test_commit_consolidation_requires_sources(mcp_impl):
    r = mcp_impl.commit_consolidation(
        draft_text="some synthesis",
        source_short_ids=[],
    )
    assert r["ok"] is False
    assert "source_short_ids" in r["error"]
