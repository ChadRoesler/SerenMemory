"""
The pull (29 Oct 2026): a superseded planet loses its gravity.

When a core is replaced, its satellites move under the new core. Each is
tested against the new core: one at least as near to it as it was to the old
one still supports what is true now and keeps its pull (carry); one that was
about the old state hangs on as history with a broken pull. The demoted core
itself is always history.

- the supersede sorts the moved satellites and says how many carried
- a carried satellite still lifts the new core in recall (matched_via)
- a history satellite never lifts it, and neither does the demoted core
- history is still there: in the surroundings, and with include_satellites
- the harness switch HISTORY_LIFTS brings the old behaviour back, in-process
"""
from __future__ import annotations

import pytest

import seren_memory.routes.search as search_mod
from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False)))


def _core(client, text, topic="t"):
    did = client.post("/drafts", json={"operations": [{"kind": "new_core", "content": text, "topic": topic}]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]["long_term_id"]


def _attach(client, core, text):
    did = client.post("/drafts", json={"operations": [{"kind": "attach", "content": text, "topic": "t", "target_core_id": core}]}).json()["id"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve", "restate": False}]}).json()
    return r["results"][0]["satellite_id"]


def _supersede(client, old, text):
    did = client.post("/drafts", json={"operations": [{"kind": "supersede", "content": text, "topic": "t", "target_core_id": old}]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]


def _search(client, q, **kw):
    return client.post("/search", json={"query": q, "n_results": 5, "include_short": False, "include_near": False, **kw}).json()["hits"]


@pytest.fixture
def replaced(client):
    old = _core(client, "The user's favourite colour is blue.")
    history = _attach(client, old, "He painted the whole minis cabinet cobalt blue in March.")
    carry = _attach(client, old, "Yellow is the user's favourite colour since spring.")
    res = _supersede(client, old, "The user's favourite colour is yellow now.")
    return {"old": old, "new": res["long_term_id"], "history": history, "carry": carry, "result": res}


def test_the_supersede_sorts_the_moved_satellites_by_pull(client, replaced):
    r = replaced["result"]
    assert r["moved_satellites"] == 2 and r["carried"] == 1 and r["history"] == 1, r
    h = client.get(f"/long/{replaced['new']}/satellites").json()
    by = {s["id"]: s["metadata"] for s in h["satellites"]}
    assert by[replaced["carry"]]["carry"] is True and by[replaced["carry"]]["moved_from"] == replaced["old"]
    assert by[replaced["history"]]["carry"] is False
    assert by[replaced["history"]]["pull_new"] > by[replaced["history"]]["pull_old"], "farther from the new core than the old"
    assert by[replaced["carry"]]["pull_new"] <= by[replaced["carry"]]["pull_old"]
    assert by[replaced["old"]]["was_core"] is True and by[replaced["old"]]["carry"] is False
    assert h["count"] == 3, "all three hang under the new core: the trail is whole"


def test_a_carried_satellite_lifts_the_new_core_and_history_does_not(client, replaced):
    for _ in range(6):
        _core(client, "An unrelated note about the weather and the garden.")
    hits = _search(client, "favourite colour since spring")
    got = next(h for h in hits if h["id"] == replaced["new"])
    assert got["matched_via"]["id"] == replaced["carry"], "still supports what is true now: it carries"

    hits = _search(client, "painted the whole minis cabinet cobalt blue in March")
    lifted = [h for h in hits if h["id"] == replaced["new"] and h["matched_via"]]
    assert not lifted, "a question about the old state does not drag the current core in"
    assert replaced["old"] not in [h["id"] for h in hits], "and the old core is never the answer"
    with_sats = _search(client, "painted the whole minis cabinet cobalt blue in March", include_satellites=True)
    assert replaced["history"] in [h["id"] for h in with_sats], "history is still there when asked for"


def test_the_harness_switch_restores_the_old_lift(client, replaced, monkeypatch):
    for _ in range(6):
        _core(client, "An unrelated note about the weather and the garden.")
    monkeypatch.setattr(search_mod, "HISTORY_LIFTS", True)
    hits = _search(client, "painted the whole minis cabinet cobalt blue in March")
    got = next(h for h in hits if h["id"] == replaced["new"])
    assert got["matched_via"]["id"] == replaced["history"], "the A/B arm: history lifts again"
