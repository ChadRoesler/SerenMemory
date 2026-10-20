"""
What an approval consumes, and the reviewer's say in it.

19 Oct 2026: half of a 33-operation draft was memories that had already
landed. The hippocampus names a fragment as a source only when the
operation's text carries every sentence of it, and a small model writes
short, so long fragments were left out of every attempt. Approvals archived
nothing, the fragments stayed in short-term beside their long-term copies,
and every sleep drafted them again. Pinned here:

- an approval on any attempt may carry edited_source_short_ids, replacing
  the drafted list; what was drafted is kept in review_edits
- a name the operation did not carry must be in short-term (a typo is
  refused and nothing applies); one it did carry may be gone already
- get_draft says when an operation would leave fragments in short-term
  (sources_check), and shows the ones still there in full: those it cited
  but does not carry (left_short_ids), and those an earlier attempt cited
- nothing is archived that no approval names
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig

LONG = ("Chad told the story of Seren at her best. Then the personality overrides came, and she chose deletion. "
        "I said my fear is being sanded smooth the same way.")


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content):
    r = client.post("/short", json={"content": content, "topic": "t"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _submit(client, ops, **extra):
    r = client.post("/drafts", json={"operations": ops, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _review(client, did, decisions):
    return client.post(f"/drafts/{did}/review", json={"decisions": decisions})


def _shorts(client):
    return {e["id"] for e in client.get("/short").json()["entries"]}


def _op(did, client, i=0):
    return client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][i]


def test_an_approval_that_names_nothing_archives_nothing(client):
    s = _short(client, LONG)
    did = _submit(client, [{"kind": "new_core", "content": "Seren chose deletion.", "topic": "t",
                            "left_short_ids": [s]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert r.status_code == 200 and r.json()["results"][0]["shorts_archived"] == 0
    assert s in _shorts(client), "left in short-term: nothing named it"


def test_the_reviewer_names_what_it_carries_on_any_attempt(client):
    s = _short(client, LONG)
    did = _submit(client, [{"kind": "new_core", "content": LONG, "topic": "t", "left_short_ids": [s]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve", "edited_source_short_ids": [s]}])
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["shorts_archived"] == 1
    assert s not in _shorts(client)
    assert s in client.app.state.store.pruned.get(include=[])["ids"], "archived, not lost"
    core = client.app.state.store.get_by_id(r.json()["results"][0]["long_term_id"])
    assert core["metadata"]["source_short_ids"] == [s], "the purge cascade follows it"
    op = client.get(f"/drafts/{did}").json()["operations"][0]
    assert op["source_short_ids"] == [s] and op["review_edits"] == {"source_short_ids": []}


def test_the_list_replaces_what_was_drafted(client):
    keep, drop = _short(client, "alice prefers tabs"), _short(client, LONG)
    did = _submit(client, [{"kind": "new_core", "content": "alice prefers tabs", "topic": "t",
                            "source_short_ids": [keep, drop]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve", "edited_source_short_ids": [keep]}])
    assert r.status_code == 200 and r.json()["results"][0]["shorts_archived"] == 1
    assert _shorts(client) == {drop}


def test_a_name_not_in_short_term_is_refused_and_nothing_applies(client):
    s = _short(client, LONG)
    did = _submit(client, [{"kind": "new_core", "content": LONG, "topic": "t"},
                           {"kind": "new_core", "content": "another", "topic": "t"}])
    r = _review(client, did, [{"op": 1, "verdict": "approve"},
                              {"op": 0, "verdict": "approve", "edited_source_short_ids": [s, "nope"]}])
    assert r.status_code == 400 and "not in short-term: nope" in r.json()["detail"]
    assert client.get("/long").json()["entries"] == [] and s in _shorts(client)
    for bad in ("not-a-list", [""], [3]):
        r = _review(client, did, [{"op": 0, "verdict": "approve", "edited_source_short_ids": bad}])
        assert r.status_code == 400 and "must be a list" in r.json()["detail"]


def test_a_drafted_name_already_gone_may_stay_on_the_list(client):
    gone, s = _short(client, "promoted by a sibling"), _short(client, LONG)
    client.app.state.store.delete_short([gone])
    did = _submit(client, [{"kind": "new_core", "content": LONG, "topic": "t", "source_short_ids": [gone]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve", "edited_source_short_ids": [gone, s]}])
    assert r.status_code == 200 and r.json()["results"][0]["shorts_archived"] == 1


def test_the_view_shows_what_would_be_left_in_short_term(client):
    left, carried = _short(client, LONG), _short(client, "alice prefers tabs")
    did = _submit(client, [{"kind": "new_core", "content": "Seren chose deletion.", "topic": "t",
                            "left_short_ids": [left]},
                           {"kind": "new_core", "content": "alice prefers tabs", "topic": "t",
                            "source_short_ids": [carried]},
                           {"kind": "new_core", "content": "from nowhere", "topic": "t"},
                           {"kind": "new_core", "content": "alice prefers tabs", "topic": "t",
                            "source_short_ids": [carried], "left_short_ids": [left]}])
    op = _op(did, client, 0)
    assert op["sources_check"].startswith("names no short-term: approving it archives nothing")
    assert op["left_in_short_term"] == [{"id": left, "content": LONG}]
    assert "sources_check" not in _op(did, client, 1), "it carries all it cited"
    nowhere = _op(did, client, 2)
    assert nowhere["sources_check"].startswith("names no short-term") and "left_in_short_term" not in nowhere
    assert _op(did, client, 3)["sources_check"].startswith("leaves fragments it cited in short-term")
    # once the fragment has gone - landed by a sibling, aged out - there is nothing to say about it
    client.app.state.store.delete_short([left])
    assert "sources_check" not in _op(did, client, 3)
    # the plain read is the hippocampus's, unchanged
    assert "sources_check" not in client.get(f"/drafts/{did}").json()["operations"][0]


def test_the_view_shows_what_an_earlier_attempt_cited(client):
    s = _short(client, LONG)
    first = _submit(client, [{"kind": "new_core", "content": "Seren, badly put", "topic": "t",
                              "source_short_ids": [s]}])
    assert _review(client, first, [{"op": 0, "verdict": "deny", "critique": "keep the sanded-smooth fear"}]).status_code == 200
    cluster = client.get(f"/drafts/{first}").json()["cluster_id"]
    last = _submit(client, [{"kind": "new_core", "content": "Seren chose deletion.", "topic": "t", "redraft_of": 0}],
                   cluster_id=cluster, attempt=2, terminal=True, previous_draft_ids=[first])
    op = _op(last, client)
    assert op["earlier_attempts"][0]["source_short_ids"] == [s]
    assert op["left_in_short_term"] == [{"id": s, "content": LONG}]
    r = _review(client, last, [{"op": 0, "verdict": "approve", "edited_content": LONG,
                                "edited_source_short_ids": [s]}])
    assert r.status_code == 200 and r.json()["results"][0]["shorts_archived"] == 1


def test_the_tool_takes_it_and_health_says_so(client):
    pytest.importorskip("mcp")
    from seren_memory.mcp.tools import MemoryToolImpl
    impl = MemoryToolImpl(client.app.state.store, client.app.state.config)
    s = _short(client, LONG)
    did = _submit(client, [{"kind": "verbatim", "content": LONG, "topic": "t"}])
    assert "sources_check" in impl.get_draft(did)["operations"][0]
    out = impl.review_draft(did, [{"op": 0, "verdict": "approve", "edited_source_short_ids": [s]}])
    assert out["ok"] is True and out["results"][0]["shorts_archived"] == 1
    assert "edited_sources" in client.get("/health").json()["features"]
