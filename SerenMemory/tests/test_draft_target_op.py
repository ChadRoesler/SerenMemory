"""
target_op: an operation on the core ANOTHER operation in the same draft
creates (hip-draft-deps). Seen live 28 Sept 2026: a dream drafted as one new
core with its details as attaches had no core id to name for them, so they
attached to the nearest wrong core.

Pinned here:
- attach / supersede may name target_op (a new_core's index) instead of
  target_core_id; a bad one is refused at submit, saying why
- approving the dependent needs its new core approved in the same review or
  already; otherwise the whole review is refused before anything applies
- the new core applies first, and the dependent records the resolved core id
- denying the new core while the dependent waits is fine; the dependent can
  then only be denied
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content, topic="dream"):
    r = client.post("/short", json={"content": content, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _dream(client, *, dependent_first=False):
    a = _short(client, "A dream the user described: a garden, a long table, everyone there.")
    b = _short(client, "Second dream: a nose wrinkle on a real laugh, freckles in the sun.")
    core = {"kind": "new_core", "content": "A dream the user described.", "topic": "dream", "source_short_ids": [a]}
    sat = {"kind": "attach", "content": "The face, from the second dream.", "topic": "dream",
           "source_short_ids": [b]}
    ops = [dict(sat, target_op=1), core] if dependent_first else [core, dict(sat, target_op=0)]
    r = client.post("/drafts", json={"summary": "the dream", "operations": ops})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _review(client, did, decisions, expect=200):
    r = client.post(f"/drafts/{did}/review", json={"decisions": decisions})
    assert r.status_code == expect, r.text
    return r.json()


@pytest.mark.parametrize("ops, says", [
    ([{"kind": "new_core", "content": "x"}, {"kind": "attach", "content": "y", "target_op": 5}],
     "not an operation in this draft"),
    ([{"kind": "new_core", "content": "x"}, {"kind": "attach", "content": "y", "target_op": -1}],
     "not an operation in this draft"),
    ([{"kind": "new_core", "content": "x"}, {"kind": "attach", "content": "y", "target_op": 1}],
     "points at itself"),
    ([{"kind": "verbatim", "content": "x"}, {"kind": "attach", "content": "y", "target_op": 0}],
     "not a new_core"),
    ([{"kind": "new_core", "content": "x"}, {"kind": "new_core", "content": "y", "target_op": 0}],
     "only for attach or supersede"),
    ([{"kind": "new_core", "content": "x"},
      {"kind": "attach", "content": "y", "target_op": 0, "target_core_id": "abc"}],
     "not both"),
])
def test_a_bad_target_op_is_refused_at_submit(client, ops, says):
    r = client.post("/drafts", json={"summary": "x", "operations": ops})
    assert r.status_code == 400 and says in r.text, r.text


def test_the_core_and_its_satellite_land_in_one_review(client):
    did = _dream(client)
    out = _review(client, did, [{"op": 1, "verdict": "approve"}, {"op": 0, "verdict": "approve"}])
    assert out["status"] == "reviewed" and out["approved"] == 2
    d = client.get(f"/drafts/{did}").json()
    core_id = d["operations"][0]["long_term_id"]
    assert d["operations"][1]["target_op"] == 0
    assert d["operations"][1]["target_core_id"] == core_id, "the resolved core is recorded"
    around = client.get(f"/long/{core_id}/satellites").json()
    assert around["count"] == 1 and "second dream" in around["satellites"][0]["content"]


def test_order_in_the_draft_does_not_matter(client):
    did = _dream(client, dependent_first=True)
    _review(client, did, [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"}])
    d = client.get(f"/drafts/{did}").json()
    assert d["operations"][0]["target_core_id"] == d["operations"][1]["long_term_id"]


def test_approving_the_satellite_alone_is_refused_and_nothing_applies(client):
    did = _dream(client)
    r = _review(client, did, [{"op": 1, "verdict": "approve"}], expect=400)
    assert "approve operation 0 too, or deny operation 1" in r["detail"]
    d = client.get(f"/drafts/{did}").json()
    assert [op["status"] for op in d["operations"]] == ["pending", "pending"]


def test_a_core_approved_earlier_is_enough(client):
    did = _dream(client)
    _review(client, did, [{"op": 0, "verdict": "approve"}])
    _review(client, did, [{"op": 1, "verdict": "approve"}])
    d = client.get(f"/drafts/{did}").json()
    assert d["operations"][1]["target_core_id"] == d["operations"][0]["long_term_id"]


def test_a_denied_core_leaves_its_satellite_only_deniable(client):
    did = _dream(client)
    _review(client, did, [{"op": 0, "verdict": "deny", "critique": "frame it as his dream"}])
    r = _review(client, did, [{"op": 1, "verdict": "approve"}], expect=400)
    assert "was denied" in r["detail"] and "deny operation 1" in r["detail"]
    _review(client, did, [{"op": 1, "verdict": "deny", "critique": "its core was denied"}])
    assert client.get(f"/drafts/{did}").json()["status"] == "reviewed"
