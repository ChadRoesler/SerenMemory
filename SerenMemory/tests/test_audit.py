"""
/audit: every sleep's chain end to end, and the numbers per model.

Chad, 26 Sept 2026: expose briefs, corrections and drafts, to catch drift and
to validate swapping one consolidator model for another. Pinned here: a chain
reads brief -> attempts -> verdicts / critiques / edits -> what landed; the
numbers group by the model stamped on each draft; a redraft denied again is
counted as a critique that did not take; a draft from before the stamps is
grouped as such, not guessed.
"""
from __future__ import annotations

import pytest

from seren_memory.audit import UNSTAMPED
from seren_memory.config import MemoryConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig())


def _short(client, content, topic="t"):
    return client.post("/short", json={"content": content, "topic": topic}).json()["id"]


def _stamp(served, prompt="p1"):
    return {"model_served": served, "model_name": "default", "model_prompt": prompt, "model_mode": "model"}


def _submit(client, ops, **kw):
    r = client.post("/drafts", json={"summary": "tonight", "operations": ops, **kw})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _review(client, did, decisions):
    r = client.post(f"/drafts/{did}/review", json={"decisions": decisions})
    assert r.status_code == 200, r.text


def _model(audit, key):
    return next(m for m in audit["models"] if m["model"] == key)


def test_a_chain_reads_end_to_end_and_the_numbers_are_per_model(client):
    b = client.post("/brief", json={"summary": "the fish dream was a one-off",
                                    "promote_hints": ["durst"], "noise_hints": ["fish"]}).json()["id"]
    s1, s2 = _short(client, "a"), _short(client, "b")
    a1 = _submit(client, [{"kind": "new_core", "content": "one", "source_short_ids": [s1]},
                          {"kind": "new_core", "content": "two", "source_short_ids": [s2]}],
                 brief_id_used=b, extra=_stamp("qwen-a.gguf"))
    _review(client, a1, [{"op": 0, "verdict": "approve"},
                         {"op": 1, "verdict": "deny", "critique": "invented a date"}])
    a2 = _submit(client, [{"kind": "new_core", "content": "two, again", "source_short_ids": [s2]}],
                 cluster_id=a1, attempt=2, previous_draft_ids=[a1], brief_id_used=b, extra=_stamp("qwen-a.gguf"))
    _review(client, a2, [{"op": 0, "verdict": "deny", "critique": "still invented a date"}])
    a3 = _submit(client, [{"kind": "new_core", "content": "two, third try", "source_short_ids": [s2]}],
                 cluster_id=a1, attempt=3, terminal=True, previous_draft_ids=[a1, a2], brief_id_used=b,
                 extra=_stamp("qwen-a.gguf"))
    _review(client, a3, [{"op": 0, "verdict": "approve", "edited_content": "two, as the reviewer wrote it"}])

    s3 = _short(client, "c")
    other = _submit(client, [{"kind": "new_core", "content": "three", "source_short_ids": [s3]}],
                    extra=_stamp("qwen-b.gguf"))
    _review(client, other, [{"op": 0, "verdict": "approve"}])

    audit = client.get("/audit").json()
    assert audit["chain_count"] == 2
    chain = next(c for c in audit["chains"] if c["cluster_id"] == a1)
    assert chain["brief"]["summary"] == "the fish dream was a one-off"
    assert chain["brief"]["noise_hints"] == ["fish"], "hints come back as lists, not JSON strings"
    assert [a["attempt"] for a in chain["attempts"]] == [1, 2, 3]
    assert chain["attempts"][1]["operations"][0]["critique"] == "still invented a date"
    assert chain["attempts"][2]["operations"][0]["edited_content"] == "two, as the reviewer wrote it"
    assert chain["attempts"][0]["model"]["served"] == "qwen-a.gguf"
    assert chain["outcome"] == "landed" and chain["landed"] == 2

    a = _model(audit, "qwen-a.gguf · prompt p1")
    assert a["first_reviewed"] == 2 and a["first_approved"] == 1 and a["first_pass_rate"] == 0.5
    assert a["landed"] == 2 and a["mean_attempts"] == 2.0, "one landed on attempt 1, one on attempt 3"
    assert a["redrafts_reviewed"] == 2 and a["repeated_denials"] == 1 and a["repeat_rate"] == 0.5, \
        "attempt 2 was denied again after a denial: the critique did not take"
    assert a["edited_on_approval"] == 1
    b_ = _model(audit, "qwen-b.gguf · prompt p1")
    assert b_["first_pass_rate"] == 1.0 and b_["repeated_denials"] == 0


def test_a_chain_that_ends_denied_and_one_under_review(client):
    s1 = _short(client, "a")
    d1 = _submit(client, [{"kind": "new_core", "content": "x", "source_short_ids": [s1]}],
                 terminal=True, extra=_stamp("m"))
    _review(client, d1, [{"op": 0, "verdict": "deny", "critique": "no"}])
    s2 = _short(client, "b")
    _submit(client, [{"kind": "new_core", "content": "y", "source_short_ids": [s2]}], extra=_stamp("m"))
    audit = client.get("/audit").json()
    outcomes = sorted(c["outcome"] for c in audit["chains"])
    assert outcomes == ["ended denied", "under review"]
    assert _model(audit, "m · prompt p1")["chains_ended_denied"] == 1


def test_drafts_from_before_the_stamps_are_grouped_as_such(client):
    s1 = _short(client, "a")
    _submit(client, [{"kind": "new_core", "content": "x", "source_short_ids": [s1]}])
    audit = client.get("/audit").json()
    assert audit["models"][0]["model"] == UNSTAMPED
    mech = _submit(client, [{"kind": "verbatim", "content": "v", "source_short_ids": [_short(client, "v")]}],
                   extra={"model_mode": "mechanical"})
    audit = client.get("/audit").json()
    assert any(m["model"] == "mechanical (no model)" for m in audit["models"])
    assert client.get("/audit", params={"limit": 1}).json()["chains"].__len__() == 1
    assert mech
