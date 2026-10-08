"""
A core is as near as its nearest satellite (/search, 2 Oct 2026).

Recall returns cores, and satellites were dropped from the vector rows. A
satellite is an episode, worded the way a question is worded, so it is often
the nearest row - and the core under it came back nowhere. Pinned here:

- a query that matches an episode returns that episode's CORE, carrying
  matched_via (the episode, with its distance)
- a core the search already had takes the satellite's distance when better
- with_surroundings puts the matching episode first among `recent`
- a superseded core is not lifted unless asked for
- include_satellites=true still returns the satellites themselves
"""
from __future__ import annotations

import pytest

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


def _search(client, q, **kw):
    return client.post("/search", json={"query": q, "n_results": 5, "include_short": False, "include_near": False, **kw}).json()["hits"]


def test_an_episode_brings_its_core(client):
    core = _core(client, "The user makes board games.")
    sat = _attach(client, core, "Dice Race is a push-your-luck dice race styled as an 8-bit platformer, with cheat codes.")
    for _ in range(6):                                       # other cores, so the fetch window is crowded
        _core(client, "An unrelated note about the weather and the garden.")
    hits = _search(client, "Dice Race dice race platformer cheat codes")
    ids = [h["id"] for h in hits]
    assert core in ids, ids
    got = next(h for h in hits if h["id"] == core)
    assert got["matched_via"]["id"] == sat and "Dice Race" in got["matched_via"]["content"]
    assert got["raw_distance"] == got["matched_via"]["raw_distance"]
    assert sat not in ids, "the satellite itself stays out of the answer"
    assert ids[0] == core, "and the core ranks where its episode would have"


def test_a_core_already_there_takes_the_better_distance(client):
    core = _core(client, "The user designs board games as a hobby.")
    sat = _attach(client, core, "Four Floor Slumlord is themed on Kowloon Walled City: blind bids, a 3x3 grid, polyomino rooms.")
    hits = _search(client, "Kowloon Walled City blind bids polyomino rooms")
    got = next(h for h in hits if h["id"] == core)
    assert got["matched_via"]["id"] == sat
    plain = _search(client, "board games hobby")
    mine = next(h for h in plain if h["id"] == core)
    assert mine["matched_via"] is None, "found by its own text: no episode to credit"


def test_with_surroundings_the_matching_episode_rides_first(client):
    core = _core(client, "The user's tabletop campaign stories.")
    _attach(client, core, "Lion in heat: the tank's head exploded.")
    _attach(client, core, "When the world turned to Feet: all four survivors insane, they bit the stone feet.")
    _attach(client, core, "Smell World is still owed.")
    # the test embedder is a bag of words, so ask with one episode's own words
    hits = _search(client, "When the world turned to Feet: all four survivors insane, they bit the stone feet.",
                   with_surroundings=True)
    got = next(h for h in hits if h["id"] == core)
    assert got["matched_via"] is not None, "found through an episode"
    recent = got["surroundings"]["recent"]
    assert recent[0]["id"] == got["matched_via"]["id"], "the episode that found it rides first"
    assert len(recent) == 3 and got["surroundings"]["satellites"] == 3


def test_a_superseded_core_is_not_lifted_unless_asked(client):
    old = _core(client, "The user likes blue.")
    sat = _attach(client, old, "He painted the whole minis cabinet cobalt blue in March.")
    did = client.post("/drafts", json={"operations": [{"kind": "supersede", "content": "The user likes yellow now.", "topic": "t", "target_core_id": old}]}).json()["id"]
    client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    ids = [h["id"] for h in _search(client, "painted the minis cabinet cobalt blue in March")]
    assert old not in ids and sat not in ids
    ids = [h["id"] for h in _search(client, "painted the minis cabinet cobalt blue in March", include_superseded=True)]
    assert old in ids


def test_include_satellites_still_returns_the_satellites_themselves(client):
    core = _core(client, "The model core.")
    sat = _attach(client, core, "On 26 Sept attempt 3 ran on the 4B at 44 tokens a second.")
    ids = [h["id"] for h in _search(client, "attempt 3 ran on the 4B at 44 tokens a second", include_satellites=True)]
    assert sat in ids
