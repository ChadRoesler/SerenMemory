"""
Low core orbit (29 Oct 2026; the lifecycle drawing, 30 Oct).

A short-term memory written close enough to a core is marked as orbiting
it: a hint on the short's metadata, no long-term written, nothing reviewed.

- a short near a core orbits it, with the distance; a short near nothing
  orbits nothing; a superseded core is never orbited
- recall: a short that matches the question brings the core it orbits up
  with it, as a satellite does, and the short rides as matched_via
- supersede: the orbit is re-tested against the replacing core - it moves
  when the short is at least as near to the new core, and breaks when it
  was about the old state; it is never inherited
- forget: a short orbiting a purged core is released, not forgotten
- the review view shows each source's orbit and questions an attach aimed
  where the episode does not orbit
- orbit.enabled off writes no orbit
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig, OrbitConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False)))


def _core(client, text, topic="games"):
    did = client.post("/drafts", json={"operations": [{"kind": "new_core", "content": text, "topic": topic}]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]["long_term_id"]


def _short(client, text, topic="games"):
    return client.post("/short", json={"content": text, "topic": topic}).json()["id"]


def _short_row(client, sid):
    return client.get(f"/memory/{sid}").json()


def _search(client, q, **kw):
    return client.post("/search", json={"query": q, "n_results": 6, "include_near": False, **kw}).json()["hits"]


CORE = "The user designs push-your-luck tile board games about flooding harbours."
NEAR = "The user is designing a push-your-luck tile board game about a flooding harbour tonight."
FAR = "The dentist appointment moved to Thursday afternoon."


def test_a_short_near_a_core_orbits_it_and_a_far_one_does_not(client):
    core = _core(client, CORE)
    near = _short(client, NEAR)
    far = _short(client, FAR, topic="errands")
    meta = _short_row(client, near)["metadata"]
    assert meta["orbit"] == [core], meta
    assert 0.0 <= meta["orbit_distances"][core] <= 0.45
    assert "orbit" not in _short_row(client, far)["metadata"]


def test_a_superseded_core_is_never_orbited(client):
    old = _core(client, CORE)
    did = client.post("/drafts", json={"operations": [{"kind": "supersede", "content": "The user has given up board game design for woodturning.",
                                                        "topic": "games", "target_core_id": old}]}).json()["id"]
    client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    near = _short(client, NEAR)
    assert "orbit" not in _short_row(client, near)["metadata"], "the old core is history now"


def test_a_short_in_orbit_brings_its_core_in_recall(client):
    core = _core(client, CORE)
    near = _short(client, NEAR)
    for _ in range(6):                                       # other cores crowd the long tier's window
        _core(client, "An unrelated note about the weather and the garden.", topic="weather")
    hits = _search(client, "designing a push-your-luck tile board game about a flooding harbour tonight")
    ids = [h["id"] for h in hits]
    assert near in ids, "the short is still its own hit"
    assert core in ids, ids
    got = next(h for h in hits if h["id"] == core)
    assert got["matched_via"]["id"] == near and got["matched_via"]["tier"] == "short"
    assert got["raw_distance"] == got["matched_via"]["raw_distance"], "the core takes the short's distance"


def test_the_lift_is_a_hint_and_not_a_crutch(client):
    """A core is lifted only when the short that orbits it matched the question."""
    core = _core(client, CORE)
    _short(client, NEAR)
    for _ in range(6):
        _core(client, "An unrelated note about the weather and the garden.", topic="weather")
    hits = _search(client, "dentist appointment Thursday afternoon")
    got = [h for h in hits if h["id"] == core and h.get("matched_via")]
    assert not got


def test_a_supersede_retests_the_orbit(client):
    old = _core(client, CORE)
    stays = _short(client, NEAR)                              # about the game: still true under the new core
    about_old = _short(client, "The user designs flooding harbour games and nothing else, ever.")
    assert _short_row(client, stays)["metadata"]["orbit"] == [old]
    assert _short_row(client, about_old)["metadata"]["orbit"] == [old]
    new_text = "The user designs push-your-luck tile board games about flooding harbours and haunted lighthouses now."
    did = client.post("/drafts", json={"operations": [{"kind": "supersede", "content": new_text, "topic": "games",
                                                        "target_core_id": old}]}).json()["id"]
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]
    new = r["long_term_id"]
    assert r["orbits_moved"] + r["orbits_released"] == 2, r
    m1 = _short_row(client, stays)["metadata"]
    m2 = _short_row(client, about_old)["metadata"]
    assert old not in m1.get("orbit", []) and old not in m2.get("orbit", []), "an orbit is never inherited"
    moved = [s for s, m in ((stays, m1), (about_old, m2)) if new in m.get("orbit", [])]
    assert len(moved) == r["orbits_moved"]
    if r["orbits_moved"]:
        assert new in m1.get("orbit", []), "the short that still fits moves its orbit to the new core"


def test_a_forget_releases_the_orbit_and_keeps_the_short(client):
    core = _core(client, CORE)
    near = _short(client, NEAR)
    tomb = client.post(f"/long/{core}/purge", json={"reason": "test", "purge_backups": False}).json()
    assert tomb["tombstone"]["cascade"]["orbits_released"] == 1, tomb
    row = _short_row(client, near)
    assert row["tier"] == "short" and "orbit" not in row["metadata"], "released, not forgotten"


def test_the_review_view_shows_the_orbit_and_questions_a_wrong_target(client):
    core = _core(client, CORE)
    other = _core(client, "The user's dentist is on Elm Street.", topic="errands")
    near = _short(client, NEAR)
    did = client.post("/drafts", json={"operations": [
        {"kind": "attach", "content": "", "topic": "games", "target_core_id": other, "source_short_ids": [near]},
        {"kind": "attach", "content": "", "topic": "games", "target_core_id": core, "source_short_ids": [near]},
        {"kind": "new_core", "content": "A fresh retelling of the harbour game night.", "topic": "games",
         "source_short_ids": [near]},
    ]}).json()["id"]
    view = client.get(f"/drafts/{did}", params={"review": True}).json()
    ops = view["operations"]
    assert ops[0]["orbit"][near][0]["core_id"] == core
    assert "does not orbit" in ops[0]["orbit_check"], ops[0]
    assert "orbit_check" not in ops[1], "aimed where the episode orbits: nothing to say"
    assert "an attach there may be the better shape" in ops[2]["orbit_check"]


def test_the_last_attempt_rewords_the_core_through_edited_restated_content(client):
    """The restate gap (seen 29 Oct 2026): on a terminal attach a reviewer
    put the corrected core in edited_content with "restate": true, and it
    landed as a satellite under the wrong wording. Now that is refused with
    the field named, and edited_restated_content rewords the core."""
    core = _core(client, CORE)
    ep = "On 30 Oct 2026 the user said the harbour games are really about lighthouses."
    sid = _short(client, ep)
    did = client.post("/drafts", json={"operations": [{"kind": "attach", "content": ep, "topic": "games",
                                                        "target_core_id": core, "source_short_ids": [sid]}],
                                       "terminal": True, "attempt": 3}).json()["id"]
    fixed = "The user designs push-your-luck tile board games about flooding harbours, which are really about lighthouses."
    r = client.post(f"/drafts/{did}/review", json={"decisions": [
        {"op": 0, "verdict": "approve", "restate": True, "edited_content": fixed}]})
    assert r.status_code == 400 and "edited_restated_content" in r.json()["detail"], r.text
    assert client.get(f"/memory/{core}").json()["content"] == CORE, "nothing landed"

    r = client.post(f"/drafts/{did}/review", json={"decisions": [
        {"op": 0, "verdict": "approve", "restate": True, "edited_restated_content": fixed}]})
    assert r.status_code == 200, r.text
    assert r.json()["results"][0]["restated"] is True
    row = client.get(f"/memory/{core}").json()
    assert row["content"] == fixed and row["metadata"]["restated_from"] == CORE


def test_reorbit_marks_the_shorts_written_before_the_cores(client):
    near = _short(client, NEAR)                               # written first: nothing to orbit yet
    assert "orbit" not in _short_row(client, near)["metadata"]
    core = _core(client, CORE)
    r = client.post("/short/reorbit").json()
    assert r == {"ok": True, "shorts": 1, "orbiting": 1}
    assert _short_row(client, near)["metadata"]["orbit"] == [core]


def test_strength_settles_a_tie_and_never_crosses_a_gap(client, monkeypatch):
    """ranking.STRENGTH: a core with episodes and orbiting shorts behind it
    gains a capped nudge; off by default."""
    from seren_memory import ranking
    a = _core(client, "The user starts fires the Scout way, with a tinder bundle and a feather stick.")
    b = _core(client, "The user starts fires the Scout way, with a tinder bundle and a feather stick, he says.")
    for t in ("Lit the fire pit with a feather stick again.", "Tinder bundle first, every time.", "Scout fire tonight."):
        did = client.post("/drafts", json={"operations": [{"kind": "attach", "content": t, "topic": "games", "target_core_id": a}]}).json()["id"]
        client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve", "restate": False}]})
    q = "the Scout way to start a fire: tinder bundle, feather stick"
    before = {h["id"]: h["score"] for h in _search(client, q, include_short=False)}
    assert a in before and b in before
    monkeypatch.setattr(ranking, "STRENGTH", {**ranking.STRENGTH, "satellites": 0.01, "orbit": 0.01, "cap": 0.03})
    after = {h["id"]: h["score"] for h in _search(client, q, include_short=False)}
    gain_a, gain_b = after[a] - before[a], after[b] - before[b]
    assert 0 < gain_a <= 0.03 + 1e-6 and abs(gain_b) < 1e-6, (gain_a, gain_b)
    monkeypatch.setattr(ranking, "STRENGTH", {**ranking.STRENGTH, "satellites": 10.0, "orbit": 10.0, "cap": 0.03})
    capped = {h["id"]: h["score"] for h in _search(client, q, include_short=False)}
    assert capped[a] - before[a] <= 0.03 + 1e-6, "the cap holds: strength settles ties, never a contest"


def test_orbit_can_be_switched_off(make_client):
    client = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False), orbit=OrbitConfig(enabled=False)))
    _core(client, CORE)
    near = _short(client, NEAR)
    assert "orbit" not in _short_row(client, near)["metadata"]
