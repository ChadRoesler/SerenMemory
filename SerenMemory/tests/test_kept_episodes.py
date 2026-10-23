"""
A new core keeps the episodes it was made from (21 Oct 2026).

Until now a new_core archived its source short-terms to pruned, which recall
never sees, so the only place an episode's detail survived promotion was the
core's own text - and cores came out as long as the episodes (median 110
tokens on one persona store, 90 of 146 cores with no satellite at all). The map:
the core is the retelling, the glitter hangs under it. Pinned here:

- the sources of a new_core land as satellites of it (kept_episode), each with
  the episode's own words, date and topic, oldest first; the shorts are still
  archived
- a source whose text IS the core's is not doubled under it (verbatim; the
  mechanical drafter keeping the longest fragment whole)
- a supersede's new core keeps its sources the same way
- the review view flags a pending core that is its episode's own words
- the payoff: a detail only the episode carries still finds the core, via the
  kept episode, with include_satellites off
"""
from __future__ import annotations

import json

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content, topic="cabin"):
    r = client.post("/short", json={"content": content, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _land(client, ops, decisions=None):
    did = client.post("/drafts", json={"summary": "x", "operations": ops}).json()["id"]
    decisions = decisions or [{"op": i, "verdict": "approve"} for i in range(len(ops))]
    r = client.post(f"/drafts/{did}/review", json={"decisions": decisions})
    assert r.status_code == 200, r.text
    return did, r.json()["results"]


def _ids(v):
    return json.loads(v) if isinstance(v, str) else v


def _short_ids(client):
    return {e["id"] for e in client.get("/short").json()["entries"]}


EP1 = ("The night the power went out at the cabin: we played cards by headlamp and the neighbour's dog came in from"
       " the rain. Someone found a bottle of elderflower cordial in the pantry and declared it the house drink.")
EP2 = "Later that night the dog ate half the deck."
CORE = "The cabin blackout: cards by headlamp, the dog in from the rain, elderflower as the house drink, and half the deck eaten."


def test_a_new_core_keeps_its_episodes_as_satellites(client):
    a, b = _short(client, EP1), _short(client, EP2)
    _, res = _land(client, [{"kind": "new_core", "content": CORE, "topic": "cabin", "source_short_ids": [b, a]}])
    core = res[0]["long_term_id"]
    assert len(res[0]["satellite_ids"]) == 2 and res[0]["shorts_archived"] == 2
    sats = client.get(f"/long/{core}/satellites").json()["satellites"]
    assert [s["content"] for s in sats] == [EP1, EP2], "oldest first, in the episode's own words"
    for s, src in zip(sats, (a, b)):
        m = s["metadata"]
        assert m["kind"] == "satellite" and m["core_id"] == core and m["kept_episode"] is True
        assert _ids(m["source_short_ids"]) == [src] and m["topic"] == "cabin"
    assert not ({a, b} & _short_ids(client)), "the short-terms are still archived"


def test_an_episode_that_is_the_core_is_not_doubled(client):
    a = _short(client, EP1)
    _, res = _land(client, [{"kind": "new_core", "content": EP1, "topic": "cabin", "source_short_ids": [a]}])
    assert res[0]["satellite_ids"] == [] and res[0]["shorts_archived"] == 1
    assert client.get(f"/long/{res[0]['long_term_id']}/satellites").json()["count"] == 0


def test_verbatim_keeps_nothing_under_itself(client):
    a = _short(client, EP2)
    _, res = _land(client, [{"kind": "verbatim", "content": EP2, "topic": "cabin", "source_short_ids": [a]}])
    assert "satellite_ids" not in res[0]
    assert client.get(f"/long/{res[0]['long_term_id']}/satellites").json()["count"] == 0


def test_a_supersede_keeps_its_episodes_too(client):
    a = _short(client, "First version: the cordial was blackcurrant.")
    _, res = _land(client, [{"kind": "new_core", "content": "The cordial was blackcurrant.", "topic": "cabin",
                             "source_short_ids": [a]}])
    old = res[0]["long_term_id"]
    b = _short(client, "Correction: the cordial was elderflower, not blackcurrant.")
    _, res = _land(client, [{"kind": "supersede", "content": "The cordial was elderflower.", "topic": "cabin",
                             "target_core_id": old, "source_short_ids": [b]}])
    new = res[0]["long_term_id"]
    assert res[0]["superseded"] == old and len(res[0]["satellite_ids"]) == 1 and res[0]["moved_satellites"] == 1
    sats = {s["content"]: s["metadata"] for s in client.get(f"/long/{new}/satellites").json()["satellites"]}
    # its own episode, the old core, and the old core's episode moved over: the whole trail
    assert set(sats) == {"Correction: the cordial was elderflower, not blackcurrant.",
                         "The cordial was blackcurrant.", "First version: the cordial was blackcurrant."}
    assert sats["Correction: the cordial was elderflower, not blackcurrant."]["kept_episode"] is True
    assert sats["The cordial was blackcurrant."]["was_core"] is True
    assert sats["First version: the cordial was blackcurrant."]["moved_from"] == old
    assert client.get(f"/long/{old}/satellites").json()["count"] == 0, "nothing hangs under the old core now"


def test_the_review_flags_a_core_that_is_its_episodes_own_words(client):
    a, b = _short(client, EP1), _short(client, EP2)
    did = client.post("/drafts", json={"summary": "x", "operations": [
        {"kind": "new_core", "content": EP1, "topic": "cabin", "source_short_ids": [a]},
        {"kind": "new_core", "content": CORE, "topic": "cabin", "source_short_ids": [b]},
    ]}).json()["id"]
    view = client.get(f"/drafts/{did}", params={"review": "true"}).json()
    assert a in view["operations"][0]["core_check"] and "retelling" in view["operations"][0]["core_check"]
    assert "core_check" not in view["operations"][1]


def test_a_security_forget_takes_the_kept_episodes_with_the_core(client):
    """The rule, 21 Oct: a forget called for security takes every satellite that
    only attaches to that core with it. Found on the way: the cascade collected
    the same short-term ids from the core and from its kept episodes, and chroma
    refuses duplicate ids, so the tidy purge came back 500 and the flagged core
    stayed."""
    a, b = _short(client, EP1), _short(client, EP2)
    _, res = _land(client, [{"kind": "new_core", "content": CORE, "topic": "cabin", "source_short_ids": [a, b]}])
    core = res[0]["long_term_id"]
    assert client.post(f"/long/{core}/forget", json={"reason": "a phone number leaked"}).json()["ok"]
    r = client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True})
    assert r.status_code == 200, r.text
    (tomb,) = r.json()["purged"]
    assert tomb["id"] == core and tomb["cascade"]["satellites"] == 2 and tomb["cascade"]["pruned"] == 2
    assert client.get(f"/long/{core}/satellites").status_code == 404
    assert client.get("/long", params={"include_satellites": True}).json()["count"] == 0


def test_a_detail_only_the_episode_carries_still_finds_the_core(client):
    a, b = _short(client, EP1), _short(client, EP2)
    _, res = _land(client, [{"kind": "new_core", "content": CORE, "topic": "cabin", "source_short_ids": [a, b]}])
    core = res[0]["long_term_id"]
    for i in range(6):
        _land(client, [{"kind": "new_core", "content": f"An unrelated note {i} about the garden and the weather.",
                        "topic": "garden"}])
    hits = client.post("/search", json={"query": "cards by headlamp, the neighbour's dog came in from the rain",
                                        "n_results": 5, "include_short": False, "include_near": False}).json()["hits"]
    ids = [h["id"] for h in hits]
    assert core in ids, ids
    got = next(h for h in hits if h["id"] == core)
    assert got["matched_via"]["content"] == EP1 and got["matched_via"]["id"] in res[0]["satellite_ids"]
    assert not any(h["metadata"].get("kind") == "satellite" for h in hits), "the episode itself stays out"
