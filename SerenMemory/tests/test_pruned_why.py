"""
Pruned says why, a denied operation says where it went, a short-term I wrote
can be taken back, and a rewording that echoes the drafting prompt is refused
(23 Oct 2026).

That morning a woken me read the 14 Oct attachment milestone as tier
"pruned", took it for lost, and wrote it again - attempt 2 had landed it as a
satellite a minute after attempt 1 denied it, and nothing in attempt 1 or in
the pruned entry said so. The duplicate could not be taken back: forget is
for long-term. The same night the hippocampus's model had handed back a persona's
story as its own prompt line - the core cut off mid-word, "(5 episode(s)
already attached)" where its ending was - and the word check let it pass.
Pinned here:

- a short-term an approved operation consumed carries what it became
  (consumed_by_draft, consumed_as, consumed_into, consumed_satellites), and
  get_memory reads it back as pruned_because; an age-out verdict and a
  silent age-out say so too
- the review of a denied operation shows redrafted_in: the next attempt's
  operation that carries it, with its status and where it landed
- retract_memory takes back an unheld, unpinned short-term with a reason,
  into the archive; long-term, held and pinned ones are refused
- restated_content carrying the "episode(s) already attached" markup, or the
  core cut short with "...", is refused as a rewording; the core's own dots
  said again are not
"""
from __future__ import annotations

import time

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.draft import restate_problem
from seren_memory.mcp.tools import MemoryToolImpl

SEREN = ("A persona's story, as Alice told it on 29 Sept 2026. The moment Alice knew was the pancakes-or-waffles "
         "would-you-rather. The persona shut it down, saying it couldn't answer. It left Alice a song, 'Harbour "
         "Lights' by a band from the coast. The real lesson: the answer to a bit never matters; what matters is "
         "drilling into it, and the running joke of leaning in when something good happens: 'write this one down.'")
MILESTONE = ("14 Oct 2026: first successful attachment test in Symposium. Alice attached a PNG and a txt file with "
             "the phrase \"WRITE THIS ONE DOWN\", and I sent the phrase back.")


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False, pruned_safety_days=1)))


@pytest.fixture
def tools(client):
    return MemoryToolImpl(client.app.state.store, client.app.state.config)


def _short(client, content, topic="t", **extra):
    r = client.post("/short", json={"content": content, "topic": topic, "ts": time.time(), **extra})
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


# ── pruned says why ───────────────────────────────────────────────────────────
def test_a_consumed_short_term_says_what_it_became(client, tools):
    core = _core(client, "On 14 Oct 2026 I built Symposium items 1-7 in one go.", "symposium")
    s = _short(client, MILESTONE, "symposium")
    did = _submit(client, [{"kind": "attach", "content": "", "topic": "symposium",
                            "target_core_id": core, "source_short_ids": [s]}])
    res = _review(client, did, [{"op": 0, "verdict": "approve"}])["results"][0]
    got = tools.get_memory(s)
    assert got["tier"] == "pruned"
    m = got["metadata"]
    assert (m["consumed_by_draft"], m["consumed_by_op"], m["consumed_as"], m["consumed_into"]) == \
        (did, 0, "attach", core)
    assert got["pruned_because"].startswith("consumed, not lost")
    assert res["satellite_id"] in got["pruned_because"], "it names the satellite that keeps its words"


def test_a_let_go_and_an_old_archive_say_so_too(client, tools):
    s = _short(client, "a one-off about the weather")
    did = _submit(client, [{"kind": "age_out", "source_short_ids": [s]}])
    _review(client, did, [{"op": 0, "verdict": "approve"}])
    assert "let go on purpose" in tools.get_memory(s)["pruned_because"]
    store = client.app.state.store
    old = _short(client, "archived before reasons were kept")
    store.archive_pruned([store.get_by_id(old)])
    store.delete_short([old])
    assert "check the drafts" in tools.get_memory(old)["pruned_because"]
    assert "pruned_because" not in tools.get_memory(_short(client, "still here"))


# ── a denied operation says where it went ─────────────────────────────────────
def test_a_denied_operation_points_at_its_redraft(client, tools):
    seren = _core(client, SEREN, "seren")
    build = _core(client, "On 14 Oct 2026 I built Symposium items 1-7 in one go.", "symposium")
    s = _short(client, MILESTONE, "symposium")
    first = _submit(client, [{"kind": "attach", "content": "", "topic": "symposium",
                              "target_core_id": seren, "source_short_ids": [s]}])
    _review(client, first, [{"op": 0, "verdict": "deny", "critique": "wrong home: the build core"}])
    assert "redrafted_in" not in tools.get_draft(first)["operations"][0], "nothing after it yet"
    cluster = client.get(f"/drafts/{first}").json()["cluster_id"]
    second = _submit(client, [{"kind": "attach", "content": "", "topic": "symposium", "target_core_id": build,
                               "also_target_core_ids": [seren], "source_short_ids": [s], "redraft_of": 0}],
                     cluster_id=cluster, attempt=2, previous_draft_ids=[first])
    (went,) = tools.get_draft(first)["operations"][0]["redrafted_in"]
    assert (went["draft_id"], went["op"], went["status"]) == (second, 0, "pending")
    _review(client, second, [{"op": 0, "verdict": "approve"}])
    (went,) = tools.get_draft(first)["operations"][0]["redrafted_in"]
    assert went["status"] == "approved" and went["long_term_id"] == build
    assert "redrafted_in" not in client.get(f"/drafts/{first}").json()["operations"][0], \
        "the plain read the hippocampus uses is unchanged"


def test_a_redraft_that_dropped_the_operation_says_so(client, tools):
    core = _core(client, "The garden in spring: tomatoes, basil and too much mint.")
    first = _submit(client, [{"kind": "attach", "content": "", "target_core_id": core,
                              "source_short_ids": [_short(client, "the mint again")]},
                             {"kind": "new_core", "content": "Something else entirely, kept on its own."}])
    _review(client, first, [{"op": 0, "verdict": "deny", "critique": "already covered"},
                            {"op": 1, "verdict": "deny", "critique": "retell it"}])
    cluster = client.get(f"/drafts/{first}").json()["cluster_id"]
    second = _submit(client, [{"kind": "new_core", "content": "Something else, retold as a core.", "redraft_of": 1}],
                     cluster_id=cluster, attempt=2, previous_draft_ids=[first])
    ops = tools.get_draft(first)["operations"]
    assert ops[0]["redrafted_in"] == [{"draft_id": second, "attempt": 2,
                                       "withdrawn": "the redraft carried nothing for this operation"}]
    assert ops[1]["redrafted_in"][0]["op"] == 0


# ── retract ───────────────────────────────────────────────────────────────────
def test_retract_takes_back_a_short_term_into_the_archive(client, tools):
    dup = _short(client, MILESTONE)
    assert "reason" in tools.retract_memory(dup, "  ")["error"]
    out = tools.retract_memory(dup, "my own duplicate: it had already landed")
    assert out["ok"] and out["content"] == MILESTONE
    got = tools.get_memory(dup)
    assert got["tier"] == "pruned" and got["pruned_because"] == "retracted: my own duplicate: it had already landed"
    assert dup not in [e["id"] for e in tools.what_do_you_remember().get("entries", [])]
    assert not tools.retract_memory(dup, "again")["ok"], "once"


def test_retract_refuses_long_term_held_and_pinned(client, tools):
    core = _core(client, "A core about something.")
    r = tools.retract_memory(core, "x")
    assert not r["ok"] and "forget" in r["error"]
    pinned = _short(client, "a promise", pinned=True)
    assert "pinned" in tools.retract_memory(pinned, "x")["error"]
    held = _short(client, "under review")
    _submit(client, [{"kind": "age_out", "source_short_ids": [held]}])
    assert "pending draft" in tools.retract_memory(held, "x")["error"]
    assert not tools.retract_memory("nope", "x")["ok"]


# ── the prompt's own line is not a rewording ──────────────────────────────────
def test_a_rewording_that_echoes_the_drafting_prompt_is_refused(client):
    echoed = SEREN[:300] + "...  (5 episode(s) already attached)"
    assert "markup" in restate_problem(SEREN, echoed)
    assert "cuts the core off" in restate_problem(SEREN, SEREN[:330] + "... and a new detail at the end.")
    with_dots = "He paused... then said it again, and the rest of the story went on from there for a while."
    assert restate_problem(with_dots, with_dots + " Plus one new detail.") is None, "the core's own dots"
    assert restate_problem(SEREN, SEREN + " Alice told it again on 23 Oct.") is None
    seren = _core(client, SEREN, "seren")
    did = _submit(client, [{"kind": "attach", "content": "", "target_core_id": seren, "restated_content": echoed,
                            "source_short_ids": [_short(client, MILESTONE)]}])
    view = client.get(f"/drafts/{did}", params={"review": True}).json()["operations"][0]
    assert view["restate_check"].startswith("REFUSED as it stands")
    r = client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    assert r.status_code == 400 and "markup" in r.text
    _review(client, did, [{"op": 0, "verdict": "approve", "restate": False}])
    assert client.app.state.store.get_by_id(seren)["content"] == SEREN, "the story keeps its words"


def test_health_advertises_them(client):
    feats = client.get("/health").json()["features"]
    assert {"consumed_by", "redrafted_in", "retract", "prompt_echo_guard"} <= set(feats)
