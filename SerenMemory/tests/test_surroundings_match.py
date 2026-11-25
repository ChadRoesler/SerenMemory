"""
A core's surroundings, by the question (/search, 24 Nov 2026).

with_surroundings handed every core its three NEWEST satellites, the one it
was found through moved to the front. A question about one episode of a core
with ten got the three latest, and a reader with room for more could not ask
for more: on the Vellmarch ladder the Callosum's page scored the same on detail
questions at 1,000 and at 4,000 tokens. Pinned here:

- saying nothing is what it was: three rows, newest first, no raw_distance
- surroundings_n is how many ride per core (0: none)
- surroundings_by "match": the episode the core was found through, then the
  other episodes the search matched, nearest first, then the newest to fill;
  every row says its raw_distance, None where the episode did not match
- a word this Memory does not know reads as "recent", and a field it does not
  know is ignored: a newer Callosum never gets a 422 from an older Memory
  (the Callosum's adapter relies on exactly that)
- n_results goes to 200 (a page asks by the token), and no further
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


def _search(client, q, n=5, **kw):
    r = client.post("/search", json={"query": q, "n_results": n, "include_short": False, "include_near": False, **kw})
    assert r.status_code == 200, r.text
    return r.json()["hits"]


FEET = "When the world turned to Feet all four survivors were insane and bit the stone feet"
SMELL = "Smell world put the tank inside the dung ball of the Dung Beetle Knight"


@pytest.fixture
def campaign(client):
    """One core, four episodes, written oldest to newest."""
    core = _core(client, "The user's tabletop campaign stories.")
    sats = {"feet": _attach(client, core, FEET),
            "smell": _attach(client, core, SMELL),
            "antelope": _attach(client, core, "The archer stayed in the tall grass and fired arrows at the Screaming Antelope."),
            "phoenix": _attach(client, core, "The Phoenix vomited rainbows on the glass cannon in lantern year nine.")}
    return core, sats


def test_saying_nothing_is_what_it_was(client, campaign):
    core, sats = campaign
    got = next(h for h in _search(client, FEET, with_surroundings=True) if h["id"] == core)
    recent = got["surroundings"]["recent"]
    assert [r["id"] for r in recent] == [sats["feet"], sats["phoenix"], sats["antelope"]], \
        "the episode it was found through, then the two newest"
    assert all(set(r) == {"id", "content", "created_at"} for r in recent), "no new key on the default answer"


def test_surroundings_n_is_how_many_ride(client, campaign):
    core, sats = campaign
    def recent(n):
        return next(h for h in _search(client, FEET, with_surroundings=True, surroundings_n=n)
                    if h["id"] == core)["surroundings"]
    assert [r["id"] for r in recent(1)["recent"]] == [sats["feet"]]
    assert len(recent(4)["recent"]) == 4 and len(recent(9)["recent"]) == 4, "all four, and no more than there are"
    none = recent(0)
    assert none["recent"] == [] and none["satellites"] == 4, "none inline; the count still says they exist"


def test_match_order_puts_the_episodes_the_question_matched_first(client, campaign):
    core, sats = campaign
    # n_results 1 keeps the long tier's fetch window at two rows: the Feet episode
    # (the question's own words) and the Smell world one (it shares 'the', 'world', 'of').
    q = FEET + " world of the"
    got = next(h for h in _search(client, q, n=1, with_surroundings=True, surroundings_by="match", surroundings_n=4)
               if h["id"] == core)
    recent = got["surroundings"]["recent"]
    assert recent[0]["id"] == sats["feet"] == got["matched_via"]["id"], "the episode that found it still rides first"
    assert recent[0]["raw_distance"] == got["matched_via"]["raw_distance"]
    matched = [r for r in recent if r["raw_distance"] is not None]
    assert [r["raw_distance"] for r in matched] == sorted(r["raw_distance"] for r in matched), "nearest first"
    filled = [r["id"] for r in recent if r["raw_distance"] is None]
    assert len(recent) == 4 and len(matched) >= 1
    newest_first = [sats[k] for k in ("phoenix", "antelope", "smell", "feet")]
    assert filled == [i for i in newest_first if i in filled], "what did not match fills in newest first"
    assert all(set(r) == {"id", "content", "created_at", "raw_distance"} for r in recent)


def test_match_order_brings_an_older_episode_that_recent_order_leaves_out(client):
    """The case the knob exists for: the question is about the OLD episodes of a
    core with newer ones. Recent order hands over the one it was found through
    and the two newest; match order hands over the ones the question is about."""
    core = _core(client, "The settlement's hunts.")
    lion = _attach(client, core, "The lion hunt in lantern year two ended with the survivors bleeding in the tall grass")
    grass = _attach(client, core, "In lantern year two the survivors hid in the tall grass before the lion hunt")
    for i in range(3):
        _attach(client, core, f"An unrelated later note number {i} about gathering resources and innovating.")
    q = "lion hunt lantern year two survivors tall grass"

    def recent(**kw):
        got = next(h for h in _search(client, q, with_surroundings=True, **kw) if h["id"] == core)
        return [r["id"] for r in got["surroundings"]["recent"]]

    by_date, by_match = recent(), recent(surroundings_by="match")
    assert len({lion, grass} & set(by_date)) == 1, "newest-first: only the one it was found through"
    assert set(by_match[:2]) == {lion, grass}, "match: both episodes the question is about, ahead of the newer notes"


def test_a_mode_or_a_field_this_memory_does_not_know_is_not_an_error(client, campaign):
    core, sats = campaign
    hits = _search(client, FEET, with_surroundings=True, surroundings_by="some-future-order",
                   a_field_from_the_future={"x": 1})
    recent = next(h for h in hits if h["id"] == core)["surroundings"]["recent"]
    assert [r["id"] for r in recent] == [sats["feet"], sats["phoenix"], sats["antelope"]], "read as recent"


def test_a_page_may_ask_for_two_hundred_rows_and_no_more(client, campaign):
    assert client.post("/search", json={"query": "q", "n_results": 200}).status_code == 200
    assert client.post("/search", json={"query": "q", "n_results": 201}).status_code == 422
