"""
The sleep that touches only what is aging (design note, 22 Oct 2026), on
Memory's side of the line.

Short-term is the quick access - this week's things - and it ranks above
long-term in recall, so a short-term leaves it only by aging out: promoted on
its way out, or let go. A promoted memory supports a core (it becomes a
satellite), refines one (a small change: the core is reworded), replaces one
(the core was wrong: the old core becomes the new one's satellite and its
trail moves with it), or is a new core.

Pinned here:
- GET /short/aging lists what reaches the end of its lifetime within a window,
  oldest first, with the lifetime Memory owns; pinned ones are listed and say
  so; what a pending draft holds is not
- age_out is a verdict: it names its short-terms, has no target and no text,
  and approving it archives them to pruned; the review shows them in full
- support with no text keeps the memory itself - its words and its date -
  under the core, and the core's evidence grows
- a satellite can hang on more than one core: both list it, both grow, a search
  through it finds both, and a purge of one core leaves it on the other
- a replace moves the old core's whole trail under the new core, keeps a
  shared satellite's other home, and gives the new core only its own evidence;
  the review shows what it says the old core got wrong
- the audit keeps age_outs out of the model's numbers and counts each kind
"""
from __future__ import annotations

import time

import pytest

from seren_memory.config import ConsolidatorConfig, LifetimeConfig, MemoryConfig

DAY = 24 * 3600


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1),
                                    lifetimes=LifetimeConfig(short_term_seconds=8 * DAY)))


def _short(client, content, topic="t", age_days=0.0, **extra):
    r = client.post("/short", json={"content": content, "topic": topic, "ts": time.time() - age_days * DAY, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _submit(client, ops, expect=200, **kw):
    r = client.post("/drafts", json={"summary": "tonight", "operations": ops, **kw})
    assert r.status_code == expect, r.text
    return r.json().get("id")


def _review(client, draft_id, decisions, expect=200):
    r = client.post(f"/drafts/{draft_id}/review", json={"decisions": decisions})
    assert r.status_code == expect, r.text
    return r.json()


def _core(client, content, topic="t"):
    did = _submit(client, [{"kind": "new_core", "content": content, "topic": topic}])
    return _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]["long_term_id"]


def _row(client, entry_id):
    rows = client.get("/long", params={"include_superseded": True, "include_satellites": True}).json()["entries"]
    return next((r for r in rows if r["id"] == entry_id), None)


def _sats(client, core_id):
    return client.get(f"/long/{core_id}/satellites").json()["satellites"]


# ── what is aging ─────────────────────────────────────────────────────────────
def test_aging_lists_what_ends_its_week_before_the_window_closes(client):
    old = _short(client, "from last week", age_days=9)
    edge = _short(client, "a week and a half day old", age_days=7.6)
    fresh = _short(client, "from this afternoon", age_days=0.1)
    pinned = _short(client, "a promise, pinned", age_days=8.5, pinned=True)
    got = client.get("/short/aging", params={"within_seconds": 20 * 3600}).json()
    assert got["lifetime_seconds"] == 8 * DAY
    ids = [e["id"] for e in got["entries"]]
    assert ids == [old, pinned, edge], "oldest first, the pin listed, this afternoon left alone"
    assert fresh not in ids
    assert next(e for e in got["entries"] if e["id"] == pinned)["metadata"]["pinned"] is True
    # with no window, only what is already past the line
    assert [e["id"] for e in client.get("/short/aging").json()["entries"]] == [old, pinned]


def test_aging_leaves_out_what_a_pending_draft_holds(client):
    held = _short(client, "under review already", age_days=9)
    free = _short(client, "not yet decided", age_days=9)
    _submit(client, [{"kind": "age_out", "source_short_ids": [held]}])
    assert [e["id"] for e in client.get("/short/aging").json()["entries"]] == [free]


# ── age_out ───────────────────────────────────────────────────────────────────
def test_an_age_out_names_what_it_lets_go_and_has_no_target(client):
    core = _core(client, "Something kept.")
    _submit(client, [{"kind": "age_out"}], expect=400)
    s = _short(client, "a one-off", age_days=8)
    _submit(client, [{"kind": "age_out", "source_short_ids": [s], "target_core_id": core}], expect=400)


def test_approving_an_age_out_archives_the_memory_and_writes_nothing(client):
    s = _short(client, "a one-off nobody needs again", age_days=8)
    did = _submit(client, [{"kind": "age_out", "source_short_ids": [s], "rationale": "nothing to keep"}])
    view = client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]
    assert view["letting_go"] == [{"id": s, "content": "a one-off nobody needs again", "ts": pytest.approx(view["letting_go"][0]["ts"])}]
    assert "lets these short-terms go" in view["age_out_check"]
    assert "sources_check" not in view
    before = len(client.get("/long", params={"include_satellites": True}).json()["entries"])
    res = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]
    assert res["kind"] == "age_out" and res["shorts_archived"] == 1 and res["long_term_id"] is None
    assert len(client.get("/long", params={"include_satellites": True}).json()["entries"]) == before
    assert client.get(f"/memory/{s}").json()["tier"] == "pruned"


def test_on_the_last_attempt_an_age_out_can_be_kept_under_a_core(client):
    core = _core(client, "The minis cabinet is cobalt blue.")
    s = _short(client, "Painted the cabinet's doors cobalt too.", age_days=8)
    did = _submit(client, [{"kind": "age_out", "source_short_ids": [s]}], terminal=True)
    assert "denying an age_out" in client.get(f"/drafts/{did}", params={"review": True}).json()["last_attempt"]
    res = _review(client, did, [{"op": 0, "verdict": "approve", "edited_kind": "attach",
                                  "edited_target_core_id": core}])["results"][0]
    assert res["kind"] == "attach" and res["long_term_id"] == core
    assert [x["content"] for x in _sats(client, core)] == ["Painted the cabinet's doors cobalt too."]


def test_the_last_attempt_keeps_the_bar_and_says_a_denial_is_decided_again(client):
    s = _short(client, "a one-off nobody needs again", age_days=8)
    did = _submit(client, [{"kind": "age_out", "source_short_ids": [s]}], terminal=True,
                  extra={"sleep_flow": "aging"})
    note = client.get(f"/drafts/{did}", params={"review": True}).json()["last_attempt"]
    assert "does not lower the bar" in note and "when in doubt it goes" in note
    assert "a moment that would be missed" in note, "the fun is kept without evidence behind it"
    assert "next sleep decides them again" in note and "ages out anyway" not in note
    assert "Deny only what" not in note, "the old keep-everything wording is gone"


# ── support keeps the memory itself ───────────────────────────────────────────
def test_support_with_no_text_keeps_the_memory_itself_under_the_core(client):
    core = _core(client, "Alice found red berries with faces in the woods.")
    s = _short(client, "Alice boiled some of the face berries; still fine.", age_days=8)
    did = _submit(client, [{"kind": "attach", "target_core_id": core, "source_short_ids": [s], "evidence_count": 1}])
    res = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]
    sats = _sats(client, core)
    assert [x["content"] for x in sats] == ["Alice boiled some of the face berries; still fine."]
    assert sats[0]["metadata"]["kept_episode"] is True
    assert sats[0]["metadata"]["created_at"] == pytest.approx(time.time() - 8 * DAY, abs=60), "its own date"
    assert res["satellite_ids"] == [sats[0]["id"]] and res["evidence_count"] == 2
    assert client.get(f"/memory/{s}").json()["tier"] == "pruned"


def test_an_attach_with_no_text_no_rewording_and_no_memory_is_refused(client):
    core = _core(client, "A core.")
    did = _submit(client, [{"kind": "attach", "target_core_id": core, "content": ""}])
    r = _review(client, did, [{"op": 0, "verdict": "approve"}], expect=400)
    assert "does nothing" in r["detail"]


# ── a satellite on more than one core ─────────────────────────────────────────
def test_one_episode_can_support_two_cores(client):
    a = _core(client, "The Workbench's projects live in a cabinet.", topic="workbench")
    c = _core(client, "The Inscryption mod is the first game project.", topic="inscryption")
    s = _short(client, "The Inscryption project got the first drawer in the cabinet.", age_days=8)
    did = _submit(client, [{"kind": "attach", "target_core_id": a, "also_target_core_ids": [c],
                            "source_short_ids": [s]}])
    res = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]
    sat = res["satellite_ids"][0]
    assert res["also_core_ids"] == [c]
    assert [x["id"] for x in _sats(client, a)] == [sat] and [x["id"] for x in _sats(client, c)] == [sat]
    assert _row(client, a)["metadata"]["evidence_count"] == 2 and _row(client, c)["metadata"]["evidence_count"] == 2
    hits = client.post("/search", json={"query": "Inscryption project got the first drawer in the cabinet",
                                        "n_results": 5}).json()["hits"]
    lifted = {h["id"] for h in hits if (h.get("matched_via") or {}).get("id") == sat}
    assert lifted == {a, c}, "the shared episode finds both of its cores"
    counts = {h["id"]: h["surroundings"]["satellites"] for h in hits if h["id"] in (a, c)}
    assert counts == {a: 1, c: 1}


def test_also_target_core_ids_must_be_live_cores_on_an_attach(client):
    a = _core(client, "Core a.")
    s = _short(client, "an episode", age_days=8)
    _submit(client, [{"kind": "attach", "target_core_id": a, "also_target_core_ids": ["nope"],
                      "source_short_ids": [s]}], expect=400)
    _submit(client, [{"kind": "new_core", "content": "x", "also_target_core_ids": [a]}], expect=400)


def test_the_reviewer_can_add_a_second_home_on_any_attempt(client):
    a = _core(client, "Core a.")
    c = _core(client, "Core c.")
    s = _short(client, "an episode about a and c", age_days=8)
    did = _submit(client, [{"kind": "attach", "target_core_id": a, "source_short_ids": [s]}])
    assert not client.get(f"/drafts/{did}").json()["terminal"]
    res = _review(client, did, [{"op": 0, "verdict": "approve", "edited_also_core_ids": [c]}])["results"][0]
    assert res["also_core_ids"] == [c] and len(_sats(client, c)) == 1
    op = client.get(f"/drafts/{did}").json()["operations"][0]
    assert op["review_edits"]["also_target_core_ids"] == [] and op["also_target_core_ids"] == [c]


def test_a_purge_of_one_core_leaves_a_shared_satellite_on_the_other(client):
    a = _core(client, "Core a.")
    c = _core(client, "Core c.")
    s = _short(client, "an episode about a and c", age_days=8)
    did = _submit(client, [{"kind": "attach", "target_core_id": a, "also_target_core_ids": [c], "source_short_ids": [s]}])
    sat = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]["satellite_ids"][0]
    tomb = client.post(f"/long/{a}/purge", json={"reason": "test", "purge_backups": False}).json()["tombstone"]
    assert tomb["cascade"]["satellites"] == 0 and tomb["cascade"]["satellites_kept_on_another_core"] == 1
    left = _row(client, sat)
    assert left is not None and left["metadata"]["core_id"] == c and left["metadata"]["also_core_ids"] == []
    tomb = client.post(f"/long/{c}/purge", json={"reason": "test", "purge_backups": False}).json()["tombstone"]
    assert tomb["cascade"]["satellites"] == 1 and _row(client, sat) is None


# ── replace moves the trail ───────────────────────────────────────────────────
def test_a_replace_moves_the_trail_and_the_new_core_earns_its_own_evidence(client):
    berries = _core(client, "Red berries with faces from the woods are good to eat.", topic="berries")
    other = _core(client, "Alice cooks everything found on a walk.", topic="cooking")
    for text, also in (("Alice mashed the face berries.", []), ("Alice fried some.", [other]),
                       ("They are good fermented.", [])):
        s = _short(client, text, age_days=8)
        did = _submit(client, [{"kind": "attach", "target_core_id": berries, "also_target_core_ids": also,
                                "source_short_ids": [s]}])
        _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert _row(client, berries)["metadata"]["evidence_count"] == 4
    blue = _short(client, "Three weeks later the face berries turned his skin blue.", age_days=8)
    did = _submit(client, [{"kind": "supersede", "target_core_id": berries, "source_short_ids": [blue],
                            "content": "Red berries with faces are terrible for you: they turn your skin blue.",
                            "contradicts": "Red berries with faces from the woods are good to eat.", "evidence_count": 1}])
    check = client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]["replace_check"]
    assert "moves its satellites under it as history" in check and "NOT in the core" not in check
    res = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]
    new = res["long_term_id"]
    assert res["moved_satellites"] == 3
    trail = {x["content"]: x["metadata"] for x in _sats(client, new)}
    assert set(trail) == {"Red berries with faces from the woods are good to eat.", "Alice mashed the face berries.",
                          "Alice fried some.", "They are good fermented.",
                          "Three weeks later the face berries turned his skin blue."}
    assert trail["Red berries with faces from the woods are good to eat."]["was_core"] is True
    assert all(trail[t]["moved_from"] == berries for t in ("Alice mashed the face berries.", "Alice fried some.",
                                                           "They are good fermented."))
    assert _row(client, new)["metadata"]["evidence_count"] == 1, "the old episodes were evidence for the old claim"
    # the fried one still supports the cooking core as well
    assert [x["content"] for x in _sats(client, other)] == ["Alice fried some."]
    assert trail["Alice fried some."]["core_id"] == new and trail["Alice fried some."]["also_core_ids"] == [other]


def test_the_review_says_when_a_replace_quotes_nothing_or_misquotes(client):
    core = _core(client, "The workbench is cool grey.")
    s = _short(client, "Painted the workbench light aqua.", age_days=8)
    did = _submit(client, [{"kind": "supersede", "target_core_id": core, "source_short_ids": [s],
                            "content": "The workbench is light aqua."}])
    assert "quotes NO sentence" in client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]["replace_check"]
    _review(client, did, [{"op": 0, "verdict": "deny", "critique": "a colour change is a refine"}])
    s2 = _short(client, "Painted the workbench light aqua, really.", age_days=8)
    did = _submit(client, [{"kind": "supersede", "target_core_id": core, "source_short_ids": [s2],
                            "content": "The workbench is light aqua.", "contradicts": "The workbench is made of oak."}])
    assert "NOT in the core" in client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]["replace_check"]


# ── the audit ─────────────────────────────────────────────────────────────────
def test_the_audit_counts_age_outs_apart_and_each_kind(client):
    core = _core(client, "A core.")
    a = _short(client, "let go", age_days=8)
    b = _short(client, "support", age_days=8)
    c = _short(client, "a detail", age_days=8)
    did = _submit(client, [{"kind": "age_out", "source_short_ids": [a]},
                           {"kind": "attach", "target_core_id": core, "source_short_ids": [b]},
                           {"kind": "attach", "target_core_id": core, "source_short_ids": [c],
                            "restated_content": "A core, with a detail."}],
                  extra={"model_mode": "model", "model_served": "m.gguf", "model_prompt": "p"})
    _review(client, did, [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"},
                          {"op": 2, "verdict": "deny", "critique": "no"}])
    m = next(x for x in client.get("/audit").json()["models"] if x["model"] == "m.gguf · prompt p")
    assert m["age_outs"] == {"reviewed": 1, "approved": 1}
    assert m["operations"] == 2 and m["first_reviewed"] == 2 and m["first_approved"] == 1
    assert m["by_kind"]["attach"] == {"reviewed": 1, "approved": 1, "first_reviewed": 1, "first_approved": 1}
    assert m["by_kind"]["attach_restate"]["approved"] == 0


def test_a_verbatim_denied_on_the_last_attempt_lets_go_of_its_mark(client):
    s = _short(client, "the exact words, once", age_days=8, pinned=True, verbatim=True)
    did = _submit(client, [{"kind": "verbatim", "content": "the exact words, once", "source_short_ids": [s]}],
                  terminal=True, extra={"sleep_flow": "aging"})
    res = _review(client, did, [{"op": 0, "verdict": "deny", "critique": "not worth keeping word for word"}])
    assert res["results"][0]["released_verbatim"] == [s]
    got = client.get(f"/memory/{s}").json()
    assert got["tier"] == "short" and not got["metadata"].get("pinned") and not got["metadata"].get("verbatim")
    assert s in [e["id"] for e in client.get("/short/aging").json()["entries"]], "decided again at the next sleep"


def test_a_verbatim_denied_before_the_last_attempt_keeps_its_mark(client):
    s = _short(client, "the exact words, twice", age_days=8, pinned=True, verbatim=True)
    did = _submit(client, [{"kind": "verbatim", "content": "the exact words, twice", "source_short_ids": [s]}])
    res = _review(client, did, [{"op": 0, "verdict": "deny", "critique": "redraft it"}])
    assert "released_verbatim" not in res["results"][0]
    assert client.get(f"/memory/{s}").json()["metadata"]["pinned"] is True
