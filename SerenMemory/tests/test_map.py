"""
The Map (23 Oct 2026): GET /long/map is all of long-term as a constellation.

Pinned here:
- every core and satellite is a node with a 3D position (from its embedding)
  and its text; satellites name their cores
- the links: a satellite's first core is home, every other core it supports
  is a bridge, and an older wording (a retell's, or a replaced core) is history
- the counts say how many of each; an empty store is an empty map
- the Map's 3D library is served from the package, behind the token, and
  nothing else is served from that path
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _land(client, ops, decisions=None):
    did = client.post("/drafts", json={"summary": "x", "operations": ops}).json()["id"]
    r = client.post(f"/drafts/{did}/review",
                    json={"decisions": decisions or [{"op": i, "verdict": "approve"} for i in range(len(ops))]})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_an_empty_store_is_an_empty_map(client):
    assert client.get("/long/map").json() == {"nodes": [], "links": [],
                                              "counts": {"cores": 0, "satellites": 0, "bridges": 0}}


def test_the_map_has_every_entry_placed_and_linked(client):
    cabin = _land(client, [{"kind": "new_core", "content": "The cabin blackout: cards by headlamp.", "topic": "cabin"}])[0]["long_term_id"]
    garden = _land(client, [{"kind": "new_core", "content": "The garden in spring.", "topic": "garden"}])[0]["long_term_id"]
    dog = _land(client, [{"kind": "attach", "content": "The dog ate the cards, then dug up the mint.",
                          "target_core_id": cabin, "also_target_core_ids": [garden]}],
                [{"op": 0, "verdict": "approve", "restate": False}])[0]["satellite_id"]
    _land(client, [{"kind": "retell", "target_core_id": garden, "topic": "garden",
                    "content": "The garden in spring: tomatoes, basil, too much mint, and a dog."}])
    m = client.get("/long/map").json()
    nodes = {n["id"]: n for n in m["nodes"]}
    assert nodes[cabin]["kind"] == "core" and nodes[cabin]["text"].startswith("The cabin")
    assert all(len(n["pos"]) == 3 and all(isinstance(v, float) for v in n["pos"]) for n in m["nodes"])
    assert max(abs(v) for n in m["nodes"] for v in n["pos"]) == pytest.approx(400.0)
    assert nodes[dog]["cores"] == [cabin, garden]
    old = next(n for n in m["nodes"] if n.get("was_core"))
    kinds = {(lk["source"], lk["target"]): lk["kind"] for lk in m["links"]}
    assert kinds[(dog, cabin)] == "home" and kinds[(dog, garden)] == "bridge"
    assert kinds[(old["id"], garden)] == "history", "a retell's older wording hangs under the core as history"
    assert m["counts"] == {"cores": 2, "satellites": 2, "bridges": 1}


def test_a_replaced_core_reads_as_history(client):
    old = _land(client, [{"kind": "new_core", "content": "Red berries with faces are good to eat.", "topic": "b"}])[0]["long_term_id"]
    new = _land(client, [{"kind": "supersede", "target_core_id": old, "topic": "b",
                          "content": "Red berries with faces turned Alice blue; they are terrible for you.",
                          "contradicts": "Red berries with faces are good to eat."}])[0]["long_term_id"]
    m = client.get("/long/map").json()
    assert {(lk["source"], lk["target"], lk["kind"]) for lk in m["links"]} == {(old, new, "history")}
    assert next(n for n in m["nodes"] if n["id"] == old)["superseded_by"] == new


def test_the_maps_library_is_served_and_nothing_else(client):
    r = client.get("/viewer/vendor/3d-force-graph.min.js")
    assert r.status_code == 200 and "ForceGraph3D" in r.text[:2000]
    assert r.headers["content-type"].startswith("text/javascript")
    assert client.get("/viewer/vendor/..%2Fapp.py").status_code == 404
    assert client.get("/viewer/vendor/other.js").status_code == 404
    assert "map" in client.get("/health").json()["features"]
