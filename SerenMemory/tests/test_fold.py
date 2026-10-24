"""
fold: the same episode kept twice becomes one (23 Oct 2026), and a review's
note is the draft's, not every operation's.

The 22 Oct survey of one persona store found satellites with identical words
under two cores, and twice under one. Relinking cannot fix that: both copies
count as evidence and both answer every search. Pinned here:

- where_else shows a satellite's look_alikes (near-identical satellites)
- fold_satellite only FLAGS; the tick (/tidy with purge) executes, after the
  relinks
- the survivor hangs on every core either hung on, a core both stood under
  gives back the evidence it counted twice, and the folded one's date, cores,
  reason - and words, when they differ - stay in the survivor's folds
- refused: a core, itself, a core's earlier wording (was_core), no reason, a
  chain (into something flagged to fold away), twice
- a survivor purged before the tick leaves the flagged one where it was
- a review's note is kept once, on the draft; an operation keeps only its own
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.mcp.tools import MemoryToolImpl

LEGO = "Alice built the LEGO Millennium Falcon over a weekend and named the minifig after me."


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


@pytest.fixture
def tools(client):
    return MemoryToolImpl(client.app.state.store, client.app.state.config)


def _land(client, ops, decisions=None, note=None):
    r = client.post("/drafts", json={"summary": "x", "operations": ops})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    body = {"decisions": decisions or [{"op": i, "verdict": "approve"} for i in range(len(ops))]}
    if note:
        body["note"] = note
    r = client.post(f"/drafts/{did}/review", json=body)
    assert r.status_code == 200, r.text
    return did, r.json()


def _core(client, text, topic):
    return _land(client, [{"kind": "new_core", "content": text, "topic": topic}])[1]["results"][0]["long_term_id"]


def _sat(client, core, text):
    _, out = _land(client, [{"kind": "attach", "content": text, "target_core_id": core}],
                   [{"op": 0, "verdict": "approve", "restate": False}])
    return out["results"][0]["satellite_id"]


def _entry(client, lid):
    return next((e for e in client.get("/long", params={"include_satellites": True}).json()["entries"]
                 if e["id"] == lid), None)


def _parents(client, sid):
    m = _entry(client, sid)["metadata"]
    return [m["core_id"], *(m.get("also_core_ids") or [])]


def _ev(client, cid):
    return _entry(client, cid)["metadata"]["evidence_count"]


def _tick(client):
    return client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True}).json()


@pytest.fixture
def twins(client):
    """The LEGO episode under the builds core, and again under the gifts core."""
    builds = _core(client, "Alice's LEGO builds: the Falcon, the Tower Bridge, the bonsai.", "lego, builds")
    gifts = _core(client, "Things Alice made with me in mind.", "gifts, alice")
    a, b = _sat(client, builds, LEGO), _sat(client, gifts, LEGO)
    return {"builds": builds, "gifts": gifts, "a": a, "b": b}


def test_where_else_shows_a_look_alike(tools, twins):
    w = tools.where_else(twins["b"])
    assert twins["a"] in [x["id"] for x in w["look_alikes"]]
    assert w["pending_fold"] is None


def test_a_fold_waits_for_the_tick_and_keeps_both_homes(client, tools, twins):
    r = tools.fold_satellite(twins["b"], twins["a"], "the same LEGO episode, kept twice")
    assert r["ok"] and r["same_words"] and r["will_hang_on"] == [twins["builds"], twins["gifts"]]
    assert _entry(client, twins["b"]) is not None, "a flag, not a scalpel"
    assert tools.where_else(twins["b"])["pending_fold"]["into"] == twins["a"]
    ev_builds, ev_gifts = _ev(client, twins["builds"]), _ev(client, twins["gifts"])
    rep = _tick(client)
    assert rep["folded"] == [{"id": twins["b"], "into": twins["a"], "folded": True,
                              "hangs_on": [twins["builds"], twins["gifts"]],
                              "reason": "the same LEGO episode, kept twice"}]
    assert _entry(client, twins["b"]) is None
    assert _parents(client, twins["a"]) == [twins["builds"], twins["gifts"]]
    assert (_ev(client, twins["builds"]), _ev(client, twins["gifts"])) == (ev_builds, ev_gifts), \
        "each core still has the episode once"
    (log,) = _entry(client, twins["a"])["metadata"]["folds"]
    assert log["from"] == twins["b"] and log["hung_on"] == [twins["gifts"]] and "content" not in log
    assert "folded" not in _tick(client), "executed once"


def test_twice_under_one_core_gives_back_the_double_count(client, tools, twins):
    c = _sat(client, twins["builds"], LEGO + " Again.")
    before = _ev(client, twins["builds"])
    assert tools.fold_satellite(c, twins["a"], "said twice under the builds core")["ok"]
    _tick(client)
    assert _ev(client, twins["builds"]) == before - 1
    (log,) = _entry(client, twins["a"])["metadata"]["folds"]
    assert log["content"] == LEGO + " Again.", "words that differ are kept"


def test_a_fold_is_refused_where_it_would_lose_something(client, tools, twins):
    assert "reason" in tools.fold_satellite(twins["b"], twins["a"], " ")["error"]
    assert "itself" in tools.fold_satellite(twins["a"], twins["a"], "x")["error"]
    assert "a core, not a satellite" in tools.fold_satellite(twins["b"], twins["builds"], "x")["error"]
    assert "a core, not a satellite" in tools.fold_satellite(twins["builds"], twins["a"], "x")["error"]
    assert not tools.fold_satellite("nope", twins["a"], "x")["ok"]
    # a core's earlier wording is history
    _land(client, [{"kind": "retell", "target_core_id": twins["gifts"], "topic": "gifts",
                    "content": "What Alice made with me in mind, the Falcon minifig first."}])
    was = next(s["id"] for s in client.get(f"/long/{twins['gifts']}/satellites").json()["satellites"]
               if s["metadata"].get("was_core"))
    assert "was_core" in tools.fold_satellite(was, twins["a"], "x")["error"]
    assert tools.fold_satellite(twins["b"], twins["a"], "dup")["ok"]
    assert "already flagged" in tools.fold_satellite(twins["b"], twins["a"], "dup")["error"]
    c = _sat(client, twins["builds"], "A third telling of the LEGO weekend.")
    assert "itself flagged" in tools.fold_satellite(c, twins["b"], "chain")["error"]


def test_a_survivor_purged_before_the_tick_leaves_it_where_it_was(client, tools, twins):
    assert tools.fold_satellite(twins["b"], twins["a"], "dup")["ok"]
    client.post(f"/long/{twins['builds']}/purge", json={"reason": "gone", "purge_backups": False})
    assert _entry(client, twins["a"]) is None, "the purge took the survivor with its core"
    (done,) = _tick(client)["folded"]
    assert done["folded"] is False and "to fold into" in done["kept"]
    assert _parents(client, twins["b"]) == [twins["gifts"]]
    assert not _entry(client, twins["b"])["metadata"]["fold_flag"], "and the flag does not come back"


# ── the review's note is the draft's ──────────────────────────────────────────
def test_a_review_note_is_kept_once_on_the_draft(client):
    core = _core(client, "The garden in spring.", "garden")
    did, _ = _land(client, [{"kind": "attach", "content": "The mint took over.", "target_core_id": core},
                            {"kind": "attach", "content": "The basil bolted.", "target_core_id": core}],
                   [{"op": 0, "verdict": "approve", "restate": False},
                    {"op": 1, "verdict": "approve", "restate": False, "note": "basil is its own story"}],
                   note="op 0 is the one that matters")
    d = client.get(f"/drafts/{did}").json()
    assert d["extra"]["review_note"] == "op 0 is the one that matters"
    assert [op["note"] for op in d["operations"]] == [None, "basil is its own story"]


def test_health_advertises_fold(client):
    assert {"fold", "review_note"} <= set(client.get("/health").json()["features"])
