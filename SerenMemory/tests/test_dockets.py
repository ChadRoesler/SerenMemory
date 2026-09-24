"""
The docket: what the hippocampus proposes, reviewed per operation, applied by
the store. And the purge: the forget flag executed, with a cascade and a
tombstone. Settled with Chad 23 Sept 2026 (see seren_memory.docket).

Pinned here:
- a docket is a list of operations; each gets its own verdict
- approving new_core / verbatim creates a core; attach creates a satellite
  and grows the core's evidence (and may restate it); supersede creates a
  new core and demotes the old one, which stays recallable through history
- recall returns cores only; satellites and superseded cores come back
  only when asked; /long/{id}/satellites is the surroundings
- denying needs a critique; a docket is reviewed once every op has a verdict;
  re-deciding an operation is a 409; edited_content is refused off a
  terminal docket
- purge removes the core AND its satellites AND the source shorts in pruned,
  scrubs the docket ops that touched it, removes backups, and leaves a
  tombstone with no content in it
- /tidy runs the mechanical steps and, when asked, purges flagged entries
"""
from __future__ import annotations

import json

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content, topic="pref"):
    r = client.post("/short", json={"content": content, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _submit(client, ops, **kw):
    r = client.post("/dockets", json={"summary": kw.pop("summary", "tonight"), "operations": ops, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _review(client, docket_id, decisions, expect=200):
    r = client.post(f"/dockets/{docket_id}/review", json={"decisions": decisions})
    assert r.status_code == expect, r.text
    return r.json()


def _long_ids(client, **params):
    return {e["id"]: e for e in client.get("/long", params=params).json()["entries"]}


# ── operations ───────────────────────────────────────────────────────────────

def test_new_core_and_verbatim_become_cores_and_consume_their_shorts(client):
    s1 = _short(client, "chad prefers tabs in makefiles")
    s2 = _short(client, "tabs again, in the makefile")
    s3 = _short(client, "never piss on an electric fence", topic="lesson")
    did = _submit(client, [
        {"kind": "new_core", "content": "Chad prefers tabs in makefiles.", "topic": "pref",
         "source_short_ids": [s1, s2], "evidence_count": 2, "rationale": "two nights running"},
        {"kind": "verbatim", "content": "never piss on an electric fence", "topic": "lesson",
         "source_short_ids": [s3]},
    ])
    out = _review(client, did, [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"}])
    assert out["status"] == "reviewed" and out["approved"] == 2
    longs = _long_ids(client)
    assert len(longs) == 2
    verbatim = next(e for e in longs.values() if e["content"] == "never piss on an electric fence")
    assert verbatim["metadata"]["kind"] == "core" and verbatim["metadata"]["preserved_verbatim"] is True
    assert client.get("/short").json()["count"] == 0, "approved operations consume their shorts"
    pruned = client.app.state.store.pruned.get(include=[])["ids"]
    assert set(pruned) == {s1, s2, s3}, "consumed shorts are archived, not lost"


def test_attach_adds_a_satellite_grows_the_core_and_may_restate_it(client):
    core = _submit(client, [{"kind": "new_core", "content": "Chad likes blue.", "topic": "color",
                             "evidence_count": 2}])
    core_id = _review(client, core, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    s = _short(client, "picked the blue theme again")
    att = _submit(client, [{"kind": "attach", "target_core_id": core_id,
                            "content": "Picked the blue theme again on 23 Sept.",
                            "restated_content": "Chad likes blue; he picks it every time.",
                            "source_short_ids": [s], "evidence_count": 1}])
    res = _review(client, att, [{"op": 0, "verdict": "approve"}])["results"][0]
    assert res["kind"] == "attach" and res["restated"] and res["evidence_count"] == 3
    longs = _long_ids(client)
    assert list(longs) == [core_id], "the satellite is not listed as a core"
    assert longs[core_id]["content"] == "Chad likes blue; he picks it every time."
    assert longs[core_id]["metadata"]["restated_from"] == "Chad likes blue."
    around = client.get(f"/long/{core_id}/satellites").json()
    assert around["count"] == 1 and around["satellites"][0]["metadata"]["core_id"] == core_id
    assert around["satellites"][0]["id"] == res["satellite_id"]
    hits = client.post("/search", json={"query": "blue theme", "n_results": 5}).json()["hits"]
    assert {h["id"] for h in hits if h["tier"] == "long"} == {core_id}, "recall returns the core, not the satellite"
    hits = client.post("/search", json={"query": "blue theme", "n_results": 5,
                                        "include_satellites": True}).json()["hits"]
    assert res["satellite_id"] in {h["id"] for h in hits}


def test_supersede_keeps_the_old_core_demoted_and_recallable_as_history(client):
    old = _review(client, _submit(client, [{"kind": "new_core", "content": "Chad likes blue.", "topic": "color"}]),
                  [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    new = _review(client, _submit(client, [{"kind": "supersede", "target_core_id": old,
                                            "content": "Chad likes yellow now.", "topic": "color"}]),
                  [{"op": 0, "verdict": "approve"}])["results"][0]
    assert new["superseded"] == old
    live = _long_ids(client)
    assert list(live) == [new["long_term_id"]]
    hist = _long_ids(client, include_superseded=True)
    assert hist[old]["metadata"]["superseded_by"] == new["long_term_id"]
    assert hist[old]["content"] == "Chad likes blue.", "blue is still there, demoted"
    hits = client.post("/search", json={"query": "favourite colour", "n_results": 5,
                                        "include_superseded": True}).json()["hits"]
    assert {old, new["long_term_id"]} <= {h["id"] for h in hits}
    around = client.get(f"/long/{new['long_term_id']}/satellites").json()
    assert around["supersedes"]["id"] == old


# ── review discipline ────────────────────────────────────────────────────────

def test_each_operation_gets_its_own_verdict_and_a_denial_needs_a_critique(client):
    did = _submit(client, [
        {"kind": "new_core", "content": "A", "topic": "t"},
        {"kind": "new_core", "content": "B", "topic": "t"},
        {"kind": "new_core", "content": "C", "topic": "t"},
    ])
    _review(client, did, [{"op": 1, "verdict": "deny"}], expect=400)
    out = _review(client, did, [{"op": 0, "verdict": "approve"},
                                {"op": 1, "verdict": "deny", "critique": "B conflates two things; split it"}])
    assert out["status"] == "pending" and out["approved"] == 1 and out["denied"] == 1 and out["pending"] == 1
    d = client.get(f"/dockets/{did}").json()
    assert d["operations"][1]["critique"].startswith("B conflates")
    assert d["operations"][1]["status"] == "denied" and d["operations"][2]["status"] == "pending"
    _review(client, did, [{"op": 0, "verdict": "approve"}], expect=409)   # already approved: a conflict, not a bad request
    out = _review(client, did, [{"op": 2, "verdict": "approve"}])
    assert out["status"] == "reviewed"
    _review(client, did, [{"op": 2, "verdict": "approve"}], expect=409)   # docket closed
    assert len(_long_ids(client)) == 2
    assert client.get("/dockets", params={"status": "pending"}).json()["count"] == 0
    assert client.get("/dockets", params={"status": "reviewed"}).json()["count"] == 1


def test_edited_content_only_on_a_terminal_docket(client):
    did = _submit(client, [{"kind": "new_core", "content": "rough", "topic": "t"}])
    _review(client, did, [{"op": 0, "verdict": "approve", "edited_content": "polished"}], expect=400)
    term = _submit(client, [{"kind": "new_core", "content": "rough", "topic": "t"}],
                   terminal=True, cluster_id=did, attempt=3, previous_docket_ids=[did])
    out = _review(client, term, [{"op": 0, "verdict": "approve", "edited_content": "polished"}])
    entry = _long_ids(client)[out["results"][0]["long_term_id"]]
    assert entry["content"] == "polished" and entry["metadata"]["original_op_content"] == "rough"
    chain = client.get(f"/dockets/{term}/chain").json()
    assert [a["attempt"] for a in chain["attempts"]] == [1, 3]


def test_submit_refuses_a_bad_target(client):
    r = client.post("/dockets", json={"operations": [{"kind": "attach", "content": "x"}]})
    assert r.status_code == 400 and "target_core_id" in r.text
    r = client.post("/dockets", json={"operations": [{"kind": "supersede", "target_core_id": "nope", "content": "x"}]})
    assert r.status_code == 400 and "nope" in r.text
    assert client.get("/dockets/nope").status_code == 404


# ── purge ────────────────────────────────────────────────────────────────────

def test_purge_takes_the_core_its_satellites_its_sources_and_the_backups(client, tmp_path):
    s1 = _short(client, "ssh-rsa AAAA... the actual key", topic="oops")
    core = _review(client, _submit(client, [{"kind": "new_core", "content": "Chad's key is ssh-rsa AAAA...",
                                             "topic": "oops", "source_short_ids": [s1]}]),
                   [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    s2 = _short(client, "used the key again", topic="oops")
    _review(client, _submit(client, [{"kind": "attach", "target_core_id": core, "content": "used it again",
                                      "source_short_ids": [s2]}]), [{"op": 0, "verdict": "approve"}])
    store = client.app.state.store
    persist = store._config.resolved_persist_dir()
    fake_backup = persist.parent / (persist.name + "_20260101000000")
    fake_backup.mkdir()
    (fake_backup / "leak.bin").write_text("ssh-rsa AAAA...")
    assert len(store.satellites_of(core)) == 1
    assert set(store.pruned.get(include=[])["ids"]) == {s1, s2}

    r = client.post(f"/long/{core}/purge", json={"reason": "leaked ssh key"})
    assert r.status_code == 200, r.text
    tomb = r.json()["tombstone"]
    assert tomb["cascade"] == {"satellites": 1, "shorts": 0, "pruned": 2, "drafts": 0,
                               "dockets_scrubbed": 2, "backups_removed": 1, "backups_retained": 0}
    assert "ssh-rsa" not in json.dumps(tomb), "a tombstone never carries content"
    assert not fake_backup.exists()
    assert _long_ids(client, include_satellites=True) == {}
    assert store.pruned.get(include=[])["ids"] == []
    for d in client.get("/dockets", params={"status": "reviewed"}).json()["entries"]:
        for op in d["operations"]:
            assert "ssh-rsa" not in (op["content"] or "") and op["status"] == "approved", "verdicts kept, content gone"
    stones = client.get("/tombstones").json()
    assert stones["count"] == 1 and stones["entries"][0]["id"] == core and stones["entries"][0]["reason"] == "leaked ssh key"
    assert client.post(f"/long/{core}/purge", json={"reason": "again"}).status_code == 404


def test_purge_needs_a_reason(client):
    core = _review(client, _submit(client, [{"kind": "new_core", "content": "x", "topic": "t"}]),
                   [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    assert client.post(f"/long/{core}/purge", json={}).status_code == 400
    assert core in _long_ids(client)


# ── tidy ─────────────────────────────────────────────────────────────────────

def test_tidy_runs_the_mechanical_steps_and_purges_flagged_entries_when_asked(client):
    core = _review(client, _submit(client, [{"kind": "new_core", "content": "flag me", "topic": "t"}]),
                   [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]
    assert client.post(f"/long/{core}/forget", json={"reason": "must not exist"}).status_code == 200
    r = client.post("/tidy", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["aged_out"] == 0 and body["near"] == {"expired": 0, "completed_promoted": 0} and body["pruned_swept"] == 0
    assert "purged" not in body, "purge is opt-in per call"
    assert core in _long_ids(client), "a flag alone removes nothing"
    body = client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True}).json()
    assert [t["id"] for t in body["purged"]] == [core]
    assert body["purged"][0]["reason"] == "must not exist"
    assert core not in _long_ids(client)


def test_age_out_leaves_the_evidence_under_a_pending_docket(client):
    """mem-ageout: the old consolidator aged out short-terms in the same run
    it drafted from them, so a redraft had nothing to work from."""
    import time as _t
    store = client.app.state.store
    s_old = _short(client, "an old fragment", topic="t")
    s_held = _short(client, "a held fragment", topic="t")
    # make both old
    for sid in (s_old, s_held):
        store.update_short_metadata(sid, {"ts": _t.time() - 10 * 24 * 3600})
    _submit(client, [{"kind": "new_core", "content": "x", "topic": "t", "source_short_ids": [s_held]}])
    aged = store.age_out_short(cutoff_seconds=8 * 24 * 3600)
    assert aged == 1
    left = {e["id"] for e in client.get("/short").json()["entries"]}
    assert left == {s_held}, "the held fragment is evidence under review"
