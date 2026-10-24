"""
relink: a satellite can find another home after it lands (23 Oct 2026).

A satellite stayed where it was born. An episode that also supported a core
made later, or one filed under the wrong core, had no way across; one persona
store had half its satellites sitting nearer another core than their own.
Pinned here:

- a review of a new or reworded core shows nearby_satellites (the existing
  episodes closest to it, with the cores they hang on), and an approval with
  link_satellite_ids hangs them under it as well, on any attempt; the core
  gains them as evidence and the operation records them
- link_satellite_ids is refused where no core is new or reworded, and for
  anything that is not a satellite
- link_satellite / unlink_satellite only FLAG; the tick (/tidy with purge)
  executes, logging the reason on the satellite (relinks)
- a move is a link and an unlink; a satellite always keeps at least one core,
  an unlink gives the evidence back, and a link to a core purged before the
  tick leaves the satellite where it was
- where_else reads both ways: a satellite's own cores against the strangers
  nearest it, and a core's nearby satellites
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.mcp.tools import MemoryToolImpl


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


@pytest.fixture
def tools(client):
    return MemoryToolImpl(client.app.state.store, client.app.state.config)


DOG = "The neighbour's dog came in from the rain, would not leave, and ate half the deck of cards."


def _land(client, ops, decisions=None, expect=200):
    r = client.post("/drafts", json={"summary": "x", "operations": ops})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    decisions = decisions or [{"op": i, "verdict": "approve"} for i in range(len(ops))]
    r = client.post(f"/drafts/{did}/review", json={"decisions": decisions})
    assert r.status_code == expect, r.text
    return did, r.json()


def _core(client, text, topic):
    return _land(client, [{"kind": "new_core", "content": text, "topic": topic}])[1]["results"][0]["long_term_id"]


def _sat(client, core, text):
    _, out = _land(client, [{"kind": "attach", "content": text, "target_core_id": core}],
                   [{"op": 0, "verdict": "approve", "restate": False}])
    return out["results"][0]["satellite_id"]


def _entry(client, lid):
    return next(e for e in client.get("/long", params={"include_satellites": True}).json()["entries"] if e["id"] == lid)


def _parents(client, sid):
    m = _entry(client, sid)["metadata"]
    return [m["core_id"], *(m.get("also_core_ids") or [])]


def _tick(client):
    return client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True}).json()


@pytest.fixture
def cabin(client):
    """A cabin core with the dog episode under it, and a garden core with its own."""
    cabin = _core(client, "The cabin blackout in October: cards by headlamp all night.", "cabin")
    dog = _sat(client, cabin, DOG)
    garden = _core(client, "The garden in spring: tomatoes, basil and too much mint.", "garden")
    _sat(client, garden, "The mint took over the whole bed by June.")
    return {"cabin": cabin, "dog": dog, "garden": garden}


def test_a_new_core_shows_the_nearby_satellites_and_its_approval_links_them(client, cabin):
    did = client.post("/drafts", json={"operations": [{
        "kind": "new_core", "topic": "dog",
        "content": "The neighbour's dog: in from the rain, would not leave, ate the deck of cards."}]}).json()["id"]
    (op,) = client.get(f"/drafts/{did}", params={"review": "true"}).json()["operations"]
    near = {s["id"]: s for s in op["nearby_satellites"]}
    assert cabin["dog"] in near and "link_satellite_ids" in op["link_check"]
    assert [c["id"] for c in near[cabin["dog"]]["hangs_on"]] == [cabin["cabin"]]
    out = client.post(f"/drafts/{did}/review", json={"decisions": [
        {"op": 0, "verdict": "approve", "link_satellite_ids": [cabin["dog"]]}]}).json()
    res = out["results"][0]
    new = res["long_term_id"]
    assert res["linked_satellite_ids"] == [cabin["dog"]]
    assert _parents(client, cabin["dog"]) == [cabin["cabin"], new], "it keeps its first home"
    assert _entry(client, new)["metadata"]["evidence_count"] == 2, "the core gains the episode as evidence"
    assert cabin["dog"] in [s["id"] for s in client.get(f"/long/{new}/satellites").json()["satellites"]]
    saved = client.get(f"/drafts/{did}").json()["operations"][0]
    assert saved["linked_satellite_ids"] == [cabin["dog"]]


def test_a_reworded_core_offers_them_too_but_leaves_out_its_own(client, cabin):
    did = client.post("/drafts", json={"operations": [{
        "kind": "retell", "target_core_id": cabin["cabin"], "topic": "cabin",
        "content": "The cabin blackout: cards by headlamp, and the neighbour's dog in from the rain."}]}).json()["id"]
    (op,) = client.get(f"/drafts/{did}", params={"review": "true"}).json()["operations"]
    assert cabin["dog"] not in [s["id"] for s in op.get("nearby_satellites") or []], \
        "a satellite already under this core is not offered back to it"


def test_link_satellite_ids_is_refused_where_no_core_is_new_or_reworded(client, cabin):
    did = client.post("/drafts", json={"operations": [
        {"kind": "attach", "content": "Another card game, by candlelight this time.",
         "target_core_id": cabin["cabin"]}]}).json()["id"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [
        {"op": 0, "verdict": "approve", "restate": False, "link_satellite_ids": [cabin["dog"]]}]})
    assert r.status_code == 400 and "link_satellite_ids" in r.text
    _, out = _land(client, [{"kind": "new_core", "content": "A core about something else.", "topic": "x"}],
                   [{"op": 0, "verdict": "approve", "link_satellite_ids": [cabin["garden"]]}], expect=400)
    assert "a core, not a satellite" in str(out)
    _, out = _land(client, [{"kind": "new_core", "content": "A core about something else.", "topic": "x"}],
                   [{"op": 0, "verdict": "approve", "link_satellite_ids": ["nope"]}], expect=400)
    assert "no long-term entry" in str(out)
    assert _parents(client, cabin["dog"]) == [cabin["cabin"]], "a refused review changes nothing"


def test_a_flagged_link_waits_for_the_tick(client, tools, cabin):
    r = tools.link_satellite(cabin["dog"], cabin["garden"], "the dog dug up the garden too")
    assert r["ok"] and r["will_hang_on"] == [cabin["cabin"], cabin["garden"]]
    assert _parents(client, cabin["dog"]) == [cabin["cabin"]], "a flag, not a scalpel"
    before = _entry(client, cabin["garden"])["metadata"]["evidence_count"]
    rep = _tick(client)
    assert rep["relinked"] == [{"id": cabin["dog"], "linked": [cabin["garden"]], "unlinked": [],
                                "reason": "the dog dug up the garden too"}]
    assert _parents(client, cabin["dog"]) == [cabin["cabin"], cabin["garden"]]
    m = _entry(client, cabin["dog"])["metadata"]
    assert not m["relink_flag"] and m["relinks"][-1]["link"] == cabin["garden"]
    assert m["relinks"][-1]["reason"] == "the dog dug up the garden too"
    assert _entry(client, cabin["garden"])["metadata"]["evidence_count"] == before + 1
    assert "relinked" not in _tick(client), "executed once"


def test_a_move_is_a_link_and_an_unlink_and_a_satellite_keeps_a_core(client, tools, cabin):
    r = tools.unlink_satellite(cabin["dog"], cabin["cabin"], "misfiled")
    assert not r["ok"] and "no core" in r["error"]
    assert tools.link_satellite(cabin["dog"], cabin["garden"], "it belongs with the garden")["ok"]
    r = tools.unlink_satellite(cabin["dog"], cabin["cabin"], "misfiled under the cabin")
    assert r["ok"] and r["will_hang_on"] == [cabin["garden"]]
    ev = _entry(client, cabin["cabin"])["metadata"]["evidence_count"]
    _tick(client)
    assert _parents(client, cabin["dog"]) == [cabin["garden"]], "its new home is now its first"
    assert _entry(client, cabin["cabin"])["metadata"]["evidence_count"] == max(1, ev - 1)
    assert cabin["dog"] not in [s["id"] for s in client.get(f"/long/{cabin['cabin']}/satellites").json()["satellites"]]


def test_a_link_to_a_core_purged_before_the_tick_leaves_it_where_it_was(client, tools, cabin):
    assert tools.link_satellite(cabin["dog"], cabin["garden"], "move it")["ok"]
    assert tools.unlink_satellite(cabin["dog"], cabin["cabin"], "move it")["ok"]
    client.post(f"/long/{cabin['garden']}/purge", json={"reason": "gone", "purge_backups": False})
    rep = _tick(client)
    (done,) = rep["relinked"]
    assert done["linked"] == [] and done["kept_on"] == [cabin["cabin"]]
    assert _parents(client, cabin["dog"]) == [cabin["cabin"]]
    assert not _entry(client, cabin["dog"])["metadata"]["relink_flag"], "and the flag does not come back every tick"


def test_a_relink_needs_a_reason_a_satellite_and_a_core(tools, cabin):
    assert "reason" in tools.link_satellite(cabin["dog"], cabin["garden"], "  ")["error"]
    assert "a core, not a satellite" in tools.link_satellite(cabin["cabin"], cabin["garden"], "x")["error"]
    assert "satellite, not a core" in tools.link_satellite(cabin["dog"], cabin["dog"], "x")["error"]
    assert "already hangs on" in tools.link_satellite(cabin["dog"], cabin["cabin"], "x")["error"]
    assert "does not hang on" in tools.unlink_satellite(cabin["dog"], cabin["garden"], "x")["error"]
    assert not tools.link_satellite("nope", cabin["garden"], "x")["ok"]


def test_where_else_reads_both_ways(client, tools, cabin):
    dog_core = _core(client, "The neighbour's dog who ate the deck of cards in the rain.", "dog")
    w = tools.where_else(cabin["dog"])
    assert w["ok"] and w["kind"] == "satellite"
    (home,) = w["hangs_on"]
    assert home["id"] == cabin["cabin"] and isinstance(home["similarity"], float)
    others = [c["id"] for c in w["nearest_other_cores"]]
    assert others[0] == dog_core and cabin["cabin"] not in others
    c = tools.where_else(dog_core)
    assert c["kind"] == "core" and cabin["dog"] in [s["id"] for s in c["nearby_satellites"]]
    assert not tools.where_else("nope")["ok"]


def test_health_advertises_relink(client):
    assert "relink" in client.get("/health").json()["features"]
