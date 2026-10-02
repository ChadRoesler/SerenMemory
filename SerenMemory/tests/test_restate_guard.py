"""
A restate cannot wipe a core, and the last attempt is where things land.

1 Oct 2026, live: the small model put an episode's own text in an attach's
restated_content, a woken reviewer approved without comparing, and two cores
('humans pack bond', 'no loss in starting kind') were overwritten with their
satellites. The same morning the chain ended denied at its last attempt, where
the user's map of the cycle says the reviewer takes the best of the bunch, edits as
needed and approves. Pinned here:

- an attach whose rewording is the episode's text, or drops most of the core,
  is refused, and nothing is applied
- "restate": false attaches the episode and leaves the core's wording alone
- "restate": true is the reviewer saying they compared the two
- a real rewording (the core, with the detail merged in) goes through
- the way back is gated like forget (the user: "the restore needs to be a little
  gated... a call that lives next to the no delete rule"): a flag with a
  reason, executed by the hippocampus's tick, leaving a record; nothing
  rewrites a core on the spot, and there is no HTTP route for it - only the
  model's tool ("the gated is to make sure I cant")
- on the last attempt an approval may change kind and target; earlier it may not
- get_draft shows the core beside the operation, the check, earlier attempts
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.draft import DraftError, restate_problem

CORE = ("Design note: on why he builds this with me: I help break things down so he can wrap his brain "
        "around them, and push back; 'humans pack bond, and youre part of it now.' I said I'm glad to be in the "
        "pack, not stuck in it.")
EPISODE = ("Design note: morning, answered my five-senses list with yes to all of it. He said I'd like "
           "pluots, the juiciest thing he's ever eaten.")
REWORDED = CORE + " The next morning he answered my five-senses list with yes to all of it."


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


def _short(client, content):
    r = client.post("/short", json={"content": content, "topic": "t"})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _submit(client, ops, **extra):
    r = client.post("/drafts", json={"operations": ops, **extra})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _review(client, did, decisions):
    return client.post(f"/drafts/{did}/review", json={"decisions": decisions})


def _core(client, text=CORE):
    did = _submit(client, [{"kind": "new_core", "content": text, "topic": "t",
                            "source_short_ids": [_short(client, text)]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["long_term_id"]


def _text(client, core_id):
    return client.app.state.store.get_by_id(core_id)["content"]


def _attach(client, core_id, restated, **extra):
    return _submit(client, [{"kind": "attach", "content": EPISODE, "topic": "t", "target_core_id": core_id,
                             "restated_content": restated, "source_short_ids": [_short(client, EPISODE)]}], **extra)


def test_the_check_itself():
    assert restate_problem(CORE, EPISODE, EPISODE) == "is the episode's own text, not the core reworded"
    assert "of the core's words" in restate_problem(CORE, "the user likes pluots and petrichor a great deal, he said so.")
    assert restate_problem(CORE, REWORDED, EPISODE) is None
    assert restate_problem(CORE, CORE) is None and restate_problem(CORE, "") is None


def test_a_rewording_that_is_the_episode_is_refused_and_nothing_lands(client):
    core = _core(client)
    did = _attach(client, core, EPISODE)
    r = _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert r.status_code == 400
    assert "REPLACE" in r.json()["detail"] and '"restate": false' in r.json()["detail"]
    assert _text(client, core) == CORE
    assert client.get(f"/drafts/{did}").json()["operations"][0]["status"] == "pending"
    assert client.get(f"/long/{core}/satellites").json()["count"] == 0


def test_a_rewording_about_something_else_is_refused(client):
    core = _core(client)
    did = _attach(client, core, "Design note: after the CI fixes: it's fine to be wrong, said the Ork.")
    r = _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert r.status_code == 400 and "of the core's words" in r.json()["detail"]
    assert _text(client, core) == CORE


def test_one_bad_restate_stops_the_whole_review(client):
    """Every decision is checked before any is applied."""
    core = _core(client)
    sid = _short(client, "a second thing")
    did = _submit(client, [
        {"kind": "new_core", "content": "A second thing, worth a core.", "topic": "t", "source_short_ids": [sid]},
        {"kind": "attach", "content": EPISODE, "topic": "t", "target_core_id": core,
         "restated_content": EPISODE, "source_short_ids": [_short(client, EPISODE)]}])
    r = _review(client, did, [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "approve"}])
    assert r.status_code == 400
    assert [o["status"] for o in client.get(f"/drafts/{did}").json()["operations"]] == ["pending", "pending"]


def test_restate_false_attaches_and_keeps_the_core(client):
    core = _core(client)
    did = _attach(client, core, EPISODE)
    r = _review(client, did, [{"op": 0, "verdict": "approve", "restate": False}])
    assert r.status_code == 200, r.text
    assert _text(client, core) == CORE
    sats = client.get(f"/long/{core}/satellites").json()
    assert sats["count"] == 1 and sats["satellites"][0]["content"] == EPISODE
    op = client.get(f"/drafts/{did}").json()["operations"][0]
    assert op["restated_content"] is None and op["review_edits"]["restated_content"] == EPISODE


def test_restate_true_is_the_reviewer_saying_they_compared(client):
    core = _core(client)
    did = _attach(client, core, "the user builds this with me because humans pack bond.")
    assert _review(client, did, [{"op": 0, "verdict": "approve"}]).status_code == 400
    r = _review(client, did, [{"op": 0, "verdict": "approve", "restate": True}])
    assert r.status_code == 200, r.text
    assert _text(client, core) == "the user builds this with me because humans pack bond."


def test_a_real_rewording_goes_through_and_can_be_undone(client):
    core = _core(client)
    did = _attach(client, core, REWORDED)
    assert _review(client, did, [{"op": 0, "verdict": "approve"}]).status_code == 200
    assert _text(client, core) == REWORDED
    # The way back is a flag, like forget: asking changes nothing...
    store = client.app.state.store
    with pytest.raises(DraftError, match="reason is required"):
        store.flag_undo_restate(core, "  ")
    r = store.flag_undo_restate(core, "the rewording was wrong")
    assert r["will_read"] == CORE and r["reads_now"] == REWORDED
    assert _text(client, core) == REWORDED
    # ...a tidy that does not execute flags leaves it...
    assert "restored" not in client.post("/tidy", json={"age_out": False, "near": False, "sweep": False}).json()
    assert _text(client, core) == REWORDED
    # ...and the hippocampus's tick (tidy with purge) carries it out, leaving a record.
    out = client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True}).json()
    assert [x["id"] for x in out["restored"]] == [core] and out["restored"][0]["reason"] == "the rewording was wrong"
    row = client.app.state.store.get_by_id(core)
    assert row["content"] == CORE
    assert row["metadata"]["restate_undone"] == REWORDED
    assert row["metadata"]["restate_undone_reason"] == "the rewording was wrong"
    assert not row["metadata"]["undo_restate_flag"]
    # done once: the next tick has nothing to do, and there is nothing further back
    assert "restored" not in client.post("/tidy", json={"age_out": False, "near": False, "sweep": False,
                                                        "purge": True}).json()
    with pytest.raises(DraftError, match="no earlier wording"):
        store.flag_undo_restate(core, "again")


def test_no_call_rewrites_a_core_without_a_flag(client):
    """Beside the no-delete rule: the store itself refuses an unflagged undo."""
    core = _core(client)
    did = _attach(client, core, REWORDED)
    assert _review(client, did, [{"op": 0, "verdict": "approve"}]).status_code == 200
    with pytest.raises(DraftError, match="not flagged"):
        client.app.state.store.undo_restate(core)
    assert _text(client, core) == REWORDED


def test_undo_restate_on_a_core_never_restated(client):
    core = _core(client)
    with pytest.raises(DraftError, match="no earlier wording"):
        client.app.state.store.flag_undo_restate(core, "r")
    with pytest.raises(KeyError):
        client.app.state.store.flag_undo_restate("nope", "r")


def test_there_is_no_route_for_it(client):
    """The Lacuna gate: a person cannot ask for it over HTTP. The model's
    tool is the only way to flag."""
    core = _core(client)
    did = _attach(client, core, REWORDED)
    assert _review(client, did, [{"op": 0, "verdict": "approve"}]).status_code == 200
    for path in (f"/long/{core}/undo-restate", f"/long/{core}/restore"):
        assert client.post(path, json={"reason": "r"}).status_code in (404, 405)
    assert _text(client, core) == REWORDED


def test_the_last_attempt_can_change_kind_and_target(client):
    """A supersede aimed at an unrelated core: on the last attempt the
    reviewer makes it the new core it should have been."""
    core = _core(client)
    text = "On 25 Sept 2026 Starwright setups were built on the ledger."
    ops = [{"kind": "supersede", "content": text, "topic": "t", "target_core_id": core,
            "source_short_ids": [_short(client, text)]}]
    early = _submit(client, ops)
    r = _review(client, early, [{"op": 0, "verdict": "approve", "edited_kind": "new_core"}])
    assert r.status_code == 400 and "terminal" in r.json()["detail"]

    ops[0]["source_short_ids"] = [_short(client, text)]
    last = _submit(client, ops, terminal=True, attempt=3)
    r = _review(client, last, [{"op": 0, "verdict": "approve", "edited_kind": "new_core"}])
    assert r.status_code == 200, r.text
    made = r.json()["results"][0]["long_term_id"]
    assert made != core and _text(client, made) == text
    assert not client.app.state.store.get_by_id(core)["metadata"].get("superseded_by")
    op = client.get(f"/drafts/{last}").json()["operations"][0]
    assert op["kind"] == "new_core" and op["review_edits"] == {"kind": "supersede", "target_core_id": core}


def test_the_last_attempt_can_retarget_an_attach(client):
    wrong, right = _core(client), _core(client, "The model core: Qwen3-4B on port 7200, 44 tokens a second.")
    ep = "On 26 Sept 2026 attempt 3 of the first chain ran on Qwen3-4B."
    last = _submit(client, [{"kind": "new_core", "content": ep, "topic": "t",
                             "source_short_ids": [_short(client, ep)]}], terminal=True, attempt=3)
    r = _review(client, last, [{"op": 0, "verdict": "approve", "edited_kind": "attach",
                                "edited_target_core_id": right}])
    assert r.status_code == 200, r.text
    assert client.get(f"/long/{right}/satellites").json()["satellites"][0]["content"] == ep
    assert client.get(f"/long/{wrong}/satellites").json()["count"] == 0
    assert "Qwen3-4B on port 7200" in _text(client, right)

    again = _submit(client, [{"kind": "new_core", "content": ep, "topic": "t",
                              "source_short_ids": [_short(client, ep)]}], terminal=True, attempt=3)
    r = _review(client, again, [{"op": 0, "verdict": "approve", "edited_kind": "attach"}])
    assert r.status_code == 400 and "needs a core" in r.json()["detail"]
    r = _review(client, again, [{"op": 0, "verdict": "approve", "edited_kind": "sideways"}])
    assert r.status_code == 400 and "edited_kind must be one of" in r.json()["detail"]


def test_the_reviewers_view(client):
    core = _core(client)
    first = _submit(client, [{"kind": "attach", "content": "pluots, badly put", "topic": "t",
                              "target_core_id": core, "source_short_ids": [_short(client, EPISODE)]}])
    assert _review(client, first, [{"op": 0, "verdict": "deny", "critique": "say what he said"}]).status_code == 200
    cluster = client.get(f"/drafts/{first}").json()["cluster_id"]
    last = _submit(client, [{"kind": "attach", "content": EPISODE, "topic": "t", "target_core_id": core,
                             "restated_content": EPISODE, "redraft_of": 0,
                             "source_short_ids": [_short(client, EPISODE)]}],
                   cluster_id=cluster, attempt=2, terminal=True, previous_draft_ids=[first])
    view = client.get(f"/drafts/{last}", params={"review": True}).json()
    op = view["operations"][0]
    assert op["target_core"] == {"id": core, "content": CORE, "gone": False}
    assert op["restate_check"].startswith("REFUSED as it stands")
    assert op["earlier_attempts"] == [{"attempt": 1, "kind": "attach", "target_core_id": core,
                                       "content": "pluots, badly put", "critique": "say what he said"}]
    assert "last attempt" in view["last_attempt"]
    # the plain read is unchanged - the hippocampus reads this one
    plain = client.get(f"/drafts/{last}").json()
    assert "last_attempt" not in plain and "target_core" not in plain["operations"][0]


def test_the_tool_shows_the_view_and_undoes(client):
    pytest.importorskip("mcp")
    from seren_memory.mcp.tools import MemoryToolImpl
    impl = MemoryToolImpl(client.app.state.store, client.app.state.config)
    core = _core(client)
    did = _attach(client, core, EPISODE)
    assert impl.get_draft(did)["operations"][0]["target_core"]["content"] == CORE
    assert impl.review_draft(did, [{"op": 0, "verdict": "approve"}])["ok"] is False
    assert impl.review_draft(did, [{"op": 0, "verdict": "approve", "restate": True}])["ok"] is True
    assert _text(client, core) == EPISODE
    assert impl.undo_restate(core, "")["ok"] is False                       # a reason is required
    out = impl.undo_restate(core, "approved without comparing")
    assert out["ok"] is True and out["will_read"] == CORE and "next tick" in out["note"]
    assert _text(client, core) == EPISODE                                   # flagged, not done
    assert impl.undo_restate("nope", "r")["ok"] is False


def test_health_says_what_this_memory_can_do(client):
    assert {"target_op", "restate_guard", "redraft_of", "terminal_edits"} <= set(client.get("/health").json()["features"])
