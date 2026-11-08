"""
Glitter that gets strong enough becomes a core (the lifecycle design,
21 Oct 2026; built 8 Nov 2026).

- the recall ledger: a satellite that was the episode behind a core in the
  final packet is counted once per search; the hot list shows it with its
  cores and count; an orbiting short is not counted
- promote: the satellite becomes a core of its own with the retelling given;
  the episode stays under the core it grew under AND under the new core;
  both cores list it; the new core says what it grew from, the satellite
  says what it became; the hot list leaves it out afterwards
- with no content the episode's own words stand, and the review view says so
- refused: a core, a core's own kept episode, a second promotion, a promote
  that names a core
- a purge forgets the count; the ledger itself bumps, merges and forgets
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.hits import HitLedger


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False)))


def _core(client, text, topic="games", sources=None):
    op = {"kind": "new_core", "content": text, "topic": topic}
    if sources:
        op["source_short_ids"] = sources
    did = client.post("/drafts", json={"operations": [op]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]["long_term_id"]


def _attach(client, core, text, topic="games"):
    did = client.post("/drafts", json={"operations": [{"kind": "attach", "content": text, "topic": topic, "target_core_id": core}]}).json()["id"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve", "restate": False}]}).json()
    return r["results"][0]["satellite_id"]


def _search(client, q, **kw):
    return client.post("/search", json={"query": q, "n_results": 5, "include_short": False, "include_near": False, **kw}).json()["hits"]


def _row(client, eid):
    return client.get(f"/memory/{eid}").json()


def _hot(client, min_hits=1):
    return client.get(f"/long/hot-satellites?min_hits={min_hits}").json()


CORE = "The user designs push-your-luck tile board games."
EPISODE = "Tidewater is a push-your-luck tile game set in a flooding harbour, with cheat codes that flood a neighbour's dock."


def test_health_says_so(client):
    f = client.get("/health").json()["features"]
    assert "promote" in f and "satellite_hits" in f


def test_the_episode_behind_a_core_is_counted_once_per_search(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    assert _hot(client)["entries"] == []
    hits = _search(client, "the flooding harbour game with cheat codes")
    assert hits and hits[0]["id"] == core and hits[0]["matched_via"]["id"] == sat
    hot = _hot(client)["entries"]
    assert len(hot) == 1 and hot[0]["id"] == sat and hot[0]["hits"] == 1 and hot[0]["cores"] == [core]
    _search(client, "cheat codes flooding the neighbour's dock")
    assert _hot(client)["entries"][0]["hits"] == 2
    assert _hot(client, min_hits=3)["entries"] == []              # the bar is the hippocampus's knob
    assert _row(client, sat)["metadata"]["kind"] == "satellite"   # nothing on the row itself


def test_an_orbiting_short_is_not_counted(client):
    # the texts test_orbit.py proves orbit each other under the test store's embedder
    core = _core(client, "The user designs push-your-luck tile board games about flooding harbours.")
    sid = client.post("/short", json={"content": "The user is designing a push-your-luck tile board game about a flooding harbour tonight.",
                                      "topic": "games"}).json()["id"]
    assert _row(client, sid)["metadata"].get("orbit") == [core]
    hits = client.post("/search", json={"query": "The user is designing a push-your-luck tile board game about a flooding harbour tonight.",
                                        "n_results": 6, "include_near": False}).json()["hits"]
    assert any(h["id"] == core and (h.get("matched_via") or {}).get("tier") == "short" for h in hits), hits
    assert _hot(client)["entries"] == []                                # an orbit is a hint, not a satellite hit


def test_promote_makes_the_episode_a_core_shared_under_the_old(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    _search(client, "the flooding harbour game with cheat codes")
    did = client.post("/drafts", json={"operations": [{
        "kind": "promote", "target_satellite_id": sat, "topic": "games",
        "content": "Tidewater: the flooding-harbour tile game, where a cheat code can drown a neighbour's dock.",
        "rationale": "recalled on its own three nights running"}]}).json()["id"]
    view = client.get(f"/drafts/{did}", params={"review": True}).json()
    op = view["operations"][0]
    assert op["target_satellite"]["id"] == sat and op["target_satellite"]["cores"] == [core]
    assert op["target_satellite"]["recall_hits"] == 1
    assert "both ways" in op["promote_check"] and "own words" not in op["promote_check"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()
    res = r["results"][0]
    new = res["long_term_id"]
    assert res["kind"] == "core" and res["promoted_from"] == sat and res["grew_under"] == [core] and new != core

    newrow = _row(client, new)
    assert newrow["metadata"]["kind"] == "core"
    assert newrow["content"].startswith("Tidewater: the flooding-harbour tile game")
    assert newrow["metadata"]["promoted_from_satellite"] == sat
    assert newrow["metadata"]["grew_under"] == [core]
    assert newrow["metadata"]["recall_hits_at_promotion"] == 1
    satrow = _row(client, sat)
    assert satrow["metadata"]["kind"] == "satellite"
    assert satrow["metadata"]["core_id"] == core and satrow["metadata"]["also_core_ids"] == [new]
    assert satrow["metadata"]["promoted_to"] == new
    assert [s["id"] for s in client.get(f"/long/{core}/satellites").json()["satellites"]] == [sat]
    assert [s["id"] for s in client.get(f"/long/{new}/satellites").json()["satellites"]] == [sat]
    assert _hot(client)["entries"] == []                           # promoted: off the hot list
    cores = {h["id"] for h in client.get("/long").json()["entries"]}
    assert {core, new} <= cores


def test_with_no_content_the_episodes_words_stand_and_the_review_says_so(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    did = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": sat}]}).json()["id"]
    op = client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]
    assert "own words" in op["promote_check"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()
    new = r["results"][0]["long_term_id"]
    assert _row(client, new)["content"] == EPISODE
    assert _row(client, new)["metadata"]["topic"] == "games"       # the satellite's topic when none was given


def test_a_terminal_approval_can_retell_it(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    did = client.post("/drafts", json={"terminal": True, "operations": [{"kind": "promote", "target_satellite_id": sat}]}).json()["id"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve",
                                                                   "edited_content": "Tidewater, the flooding-harbour game."}]}).json()
    new = r["results"][0]["long_term_id"]
    assert _row(client, new)["content"] == "Tidewater, the flooding-harbour game."
    assert _row(client, new)["metadata"]["edited_on_review"] is True


def test_what_cannot_be_promoted(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    # a core
    r = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": core}]})
    assert r.status_code == 400 and "core already" in r.text
    # a core's own kept episode
    sid = client.post("/short", json={"content": "A night of harbour tiles and too much coffee.", "topic": "games"}).json()["id"]
    kept_core = _core(client, "Harbour tiles are a late-night habit.", sources=[sid])
    kept = client.get(f"/long/{kept_core}/satellites").json()["satellites"]
    assert kept and kept[0]["metadata"].get("kept_episode")
    r = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": kept[0]["id"]}]})
    assert r.status_code == 400 and "core's own wording" in r.text
    # naming a core as well
    r = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": sat, "target_core_id": core}]})
    assert r.status_code == 400 and "not a core" in r.text
    # a satellite id on another kind
    r = client.post("/drafts", json={"operations": [{"kind": "attach", "content": "x", "target_core_id": core, "target_satellite_id": sat}]})
    assert r.status_code == 400 and "only for a promote" in r.text
    # twice
    did = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": sat}]}).json()["id"]
    client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    r = client.post("/drafts", json={"operations": [{"kind": "promote", "target_satellite_id": sat}]})
    assert r.status_code == 400 and "already promoted" in r.text


def test_a_purge_forgets_the_count(client):
    core = _core(client, CORE)
    sat = _attach(client, core, EPISODE)
    _search(client, "the flooding harbour game with cheat codes")
    assert _hot(client)["entries"][0]["id"] == sat
    r = client.post(f"/long/{sat}/purge", json={"reason": "a test's secret", "purge_backups": False})
    assert r.status_code == 200, r.text
    assert _hot(client)["entries"] == []


def test_the_ledger_itself(tmp_path):
    led = HitLedger(tmp_path / "hits.json")
    assert led.bump(["a", "b", "a"], now=10.0) == 2                 # once per call per satellite
    assert led.count("a") == 1 and led.count("zzz") == 0
    led.bump(["a"], now=20.0)
    assert [s for s, _ in led.hot(1)] == ["a", "b"]                 # most recalled first
    assert led.hot(2) and led.hot(2)[0][0] == "a" and led.get("a")["last"] == 20.0 and led.get("a")["first"] == 10.0
    led.merge("b", "a")                                             # a fold: the counts add up
    assert led.count("a") == 3 and led.get("b") is None
    assert led.forget(["a", "nope"]) == 1 and len(led) == 0
    again = HitLedger(tmp_path / "hits.json")                       # what was written is what is read back
    assert len(again) == 0
    (tmp_path / "hits.json").write_text("{not json", encoding="utf-8")
    assert len(HitLedger(tmp_path / "hits.json")) == 0              # a corrupt file starts the count over, never raises
