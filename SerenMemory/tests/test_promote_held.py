"""
promote_memory_now does not go around a review: the consolidator promotes
what is approved, and the gate exists so the model is the one who approves
its own memories.

Found the same day by a woken reviewer: promote_memory_now worked on a
short-term a pending draft still cited, which left the draft citing something
already landed. Pinned here:

- a short-term no draft holds promotes as it always did
- one cited by a PENDING operation is refused (409 over HTTP; ok false with
  held_by_draft from the tool), naming the draft
- denied in a draft whose redraft has not come back, it is still held
- once the chain is finished for it - a terminal denial, or a later attempt
  that no longer cites it - it is free again
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content):
    r = client.post("/short", json={"content": content, "topic": "t"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _draft(client, sid, **extra):
    r = client.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": "The voice card lesson.", "topic": "t", "source_short_ids": [sid]}], **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _deny(client, did):
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "deny", "critique": "no"}]})
    assert r.status_code == 200, r.text


def test_a_free_short_term_promotes_as_before(client):
    sid = _short(client, "A note nobody else cites.")
    r = client.post(f"/short/{sid}/promote")
    assert r.status_code == 200 and r.json()["long_term_id"]


def test_a_short_term_under_review_is_refused_and_the_draft_is_named(client):
    sid = _short(client, "voice card v1 over-corrected")
    did = _draft(client, sid)
    r = client.post(f"/short/{sid}/promote")
    assert r.status_code == 409 and did in r.json()["detail"] and "under review" in r.json()["detail"]
    assert client.app.state.store.shorts_under_review() == {sid: did}


def test_the_tool_says_why_and_which_draft(client):
    pytest.importorskip("mcp")
    from seren_memory.mcp.tools import MemoryToolImpl
    store = client.app.state.store
    tools = MemoryToolImpl(store, MemoryConfig())
    sid = _short(client, "voice card v1 over-corrected")
    did = _draft(client, sid)
    out = tools.promote_memory_now(sid)
    assert out["ok"] is False and out["held_by_draft"] == did and "review_draft" in out["error"]
    free = _short(client, "something no draft holds")
    assert tools.promote_memory_now(free)["ok"] is True


def test_denied_and_waiting_for_its_redraft_it_is_still_held(client):
    sid = _short(client, "voice card v1 over-corrected")
    did = _draft(client, sid, attempt=1, terminal=False)
    _deny(client, did)
    assert client.post(f"/short/{sid}/promote").status_code == 409, "the redraft will come back citing it"


def test_a_terminal_denial_frees_it(client):
    sid = _short(client, "voice card v1 over-corrected")
    did = _draft(client, sid, attempt=3, terminal=True)
    _deny(client, did)
    assert client.app.state.store.shorts_under_review() == {}
    assert client.post(f"/short/{sid}/promote").status_code == 200, "the chain is over: the escape hatch is open"


def test_a_later_attempt_that_no_longer_cites_it_frees_it(client):
    sid = _short(client, "voice card v1 over-corrected")
    other = _short(client, "something else")
    did = _draft(client, sid, attempt=1, terminal=False)
    _deny(client, did)
    nxt = _draft(client, other, attempt=2, cluster_id=did, previous_draft_ids=[did])
    assert client.app.state.store.shorts_under_review() == {other: nxt}
    assert client.post(f"/short/{sid}/promote").status_code == 200


def test_a_consumed_short_term_can_still_be_read_from_the_archive(client):
    """3 Oct 2026: a fragment archived by a core that carried half of it; the
    reviewer could not read the rest back. Now get_by_id reaches the pruned
    archive (tier 'pruned'); recall still does not."""
    sid = _short(client, "Two things in one breath: the user's backup plan, and how the keys rotate.")
    did = _draft(client, sid)
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    assert r.status_code == 200
    store = client.app.state.store
    row = store.get_by_id(sid)
    assert row and row["tier"] == "pruned" and "how the keys rotate" in row["content"] and row["metadata"].get("pruned_at")
    hits = client.post("/search", json={"query": "how the keys rotate backup plan", "n_results": 5}).json()["hits"]
    assert sid not in {h["id"] for h in hits}, "archived: not recalled"
