"""
retell: the core's wording becomes the retelling (21 Oct 2026).

The map: the core is what the person would say to 'remember that night?', a
paragraph at most; the glitter hangs under it. The 146 cores already in one persona
store were promoted as whole episodes (median 110 tokens), so they need
reshaping through the review loop, not a migration script. Pinned here:

- a retell replaces the core's wording (restated_from keeps the old), hangs the
  old wording under the core as its episode (kept_episode, was_core, dated when
  the core was), and does not touch evidence_count
- a retell that keeps the wording is refused at review; the old wording is not
  doubled under the core when a satellite already carries it
- it needs a core: a satellite target or no target is refused at submit
- the review view shows the core beside it (target_core) and a retell_check,
  and does not nag about sources, since a retell cites nothing
- the payoff: a detail only the old wording carries still finds the core, via
  the episode it became
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


FAT = ("21 Oct 2026, ~00:10: the night the power went out at the cabin. We played cards by headlamp for hours, the nei"
       "ghbour's dog came in from the rain and would not leave, and nobody could find the fuse box. Someone found a bo"
       "ttle of elderflower cordial in the pantry and declared it the house drink, and by morning the dog had eaten half the deck.")
RETOLD = "The cabin blackout: cards by headlamp, the dog in from the rain, elderflower as the house drink, and half the deck gone by morning."


def _short(client, content, topic="cabin"):
    return client.post("/short", json={"content": content, "topic": topic}).json()["id"]


def _land(client, ops, decisions=None, expect=200):
    r = client.post("/drafts", json={"summary": "x", "operations": ops})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    decisions = decisions or [{"op": i, "verdict": "approve"} for i in range(len(ops))]
    r = client.post(f"/drafts/{did}/review", json={"decisions": decisions})
    assert r.status_code == expect, r.text
    return did, r.json()


def _fat_core(client):
    a = _short(client, FAT)
    _, out = _land(client, [{"kind": "new_core", "content": FAT, "topic": "cabin", "source_short_ids": [a]}])
    return out["results"][0]["long_term_id"]


def _entry(client, lid):
    return next(e for e in client.get("/long", params={"include_satellites": True}).json()["entries"] if e["id"] == lid)


def test_a_retell_replaces_the_wording_and_keeps_the_old_as_the_episode(client):
    core = _fat_core(client)
    before = _entry(client, core)["metadata"]
    _, out = _land(client, [{"kind": "retell", "content": RETOLD, "target_core_id": core, "topic": "cabin"}])
    res = out["results"][0]
    assert res["kind"] == "retell" and res["long_term_id"] == core and res["satellite_id"]
    after = _entry(client, core)
    assert after["content"] == RETOLD and after["metadata"]["restated_from"] == FAT
    assert after["metadata"]["evidence_count"] == before["evidence_count"] == 1, "a retelling is not new evidence"
    assert after["metadata"]["kind"] == "core" and not after["metadata"].get("superseded_by")
    (sat,) = client.get(f"/long/{core}/satellites").json()["satellites"]
    assert sat["id"] == res["satellite_id"] and sat["content"] == FAT
    m = sat["metadata"]
    assert m["kept_episode"] is True and m["was_core"] is True and m["core_id"] == core
    assert m["created_at"] == before["created_at"], "the episode is dated when the core was"


def test_a_retell_that_keeps_the_wording_is_refused(client):
    core = _fat_core(client)
    _, out = _land(client, [{"kind": "retell", "content": FAT, "target_core_id": core}], expect=400)
    assert "changes nothing" in str(out)
    assert _entry(client, core)["content"] == FAT


def test_the_old_wording_is_not_doubled_under_the_core(client):
    core = _fat_core(client)
    # an attach already carries the core's own words (the 1 Oct overwrite shape)
    _land(client, [{"kind": "attach", "content": FAT, "target_core_id": core}],
          [{"op": 0, "verdict": "approve", "restate": False}])
    _, out = _land(client, [{"kind": "retell", "content": RETOLD, "target_core_id": core}])
    assert out["results"][0]["satellite_id"] is None
    sats = client.get(f"/long/{core}/satellites").json()["satellites"]
    assert [s["content"] for s in sats] == [FAT]


def test_a_retell_needs_a_core(client):
    core = _fat_core(client)
    r = client.post("/drafts", json={"operations": [{"kind": "retell", "content": RETOLD}]})
    assert r.status_code == 400 and "target_core_id" in r.text and "target_op" not in r.text
    _, out = _land(client, [{"kind": "attach", "content": "a detail", "target_core_id": core}],
                   [{"op": 0, "verdict": "approve", "restate": False}])
    sat = out["results"][0]["satellite_id"]
    r = client.post("/drafts", json={"operations": [{"kind": "retell", "content": RETOLD, "target_core_id": sat}]})
    assert r.status_code == 400 and "satellite" in r.text


def test_the_review_shows_the_core_beside_the_retelling(client):
    core = _fat_core(client)
    did = client.post("/drafts", json={"operations": [{"kind": "retell", "content": RETOLD, "target_core_id": core}]}).json()["id"]
    (op,) = client.get(f"/drafts/{did}", params={"review": "true"}).json()["operations"]
    assert op["target_core"] == {"id": core, "content": FAT, "gone": False}
    assert "REPLACES" in op["retell_check"] and "undo_restate" in op["retell_check"]
    assert "sources_check" not in op, "a retell cites nothing, by design"


def test_a_detail_only_the_old_wording_carries_still_finds_the_core(client):
    core = _fat_core(client)
    _land(client, [{"kind": "retell", "content": RETOLD, "target_core_id": core}])
    for i in range(6):
        _land(client, [{"kind": "new_core", "content": f"An unrelated note {i} about the garden and the weather.",
                        "topic": "garden"}])
    hits = client.post("/search", json={"query": "nobody could find the fuse box and the dog would not leave",
                                        "n_results": 5, "include_short": False, "include_near": False}).json()["hits"]
    got = next((h for h in hits if h["id"] == core), None)
    assert got is not None, [h["id"] for h in hits]
    assert got["content"] == RETOLD and got["matched_via"]["content"] == FAT
