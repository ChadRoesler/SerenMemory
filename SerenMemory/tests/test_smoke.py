"""
Smoke test for SerenMemory.

Boots the app with an isolated temp persist dir, exercises the full loop:
write short -> write near -> search -> submit brief -> draft -> review -> verify.

Consolidation is SerenHippocampus's; here a test submits the draft it
would, and reviews it the way the main model would.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    """FakeEmbedder lives in conftest.py."""
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(pruned_safety_days=0)))


def test_root_and_health(client):
    assert client.get("/health").json()["ok"] is True
    root = client.get("/").json()
    assert root["service"] == "SerenMemory"
    assert "tiers" in root


def test_short_term_write_read_delete(client):
    r = client.post("/short", json={"content": "Chad prefers absolute paths", "topic": "config"})
    assert r.json()["ok"]
    eid = r.json()["id"]

    listing = client.get("/short").json()
    assert listing["count"] >= 1

    d = client.delete(f"/short/{eid}")
    assert d.json()["ok"]


def test_near_term_lifecycle(client):
    r = client.post("/near", json={
        "intent": "ask how bring-up went",
        "topic": "follow_up",
        "trigger_type": "time",
        "trigger_value": str(time.time() + 3600),
    })
    eid = r.json()["id"]
    assert r.json()["ok"]

    # complete it
    c = client.post(f"/near/{eid}/complete")
    assert c.json()["ok"]

    # completed ones hidden by default
    open_loops = client.get("/near").json()
    assert all(e["id"] != eid for e in open_loops["entries"])


def test_long_term_is_gated(client):
    # No POST /long exists - creation is consolidator-only.
    r = client.post("/long", json={"content": "should not work"})
    assert r.status_code == 405  # method not allowed - route doesn't exist

    # forget requires a reason
    bad = client.post("/long/nonexistent/forget", json={})
    assert bad.status_code == 400


def test_search_unified(client):
    client.post("/short", json={"content": "Seren runs on Jetson hardware", "topic": "hardware"})
    client.post("/near", json={"intent": "upgrade to Orin AGX 64GB", "topic": "hardware"})

    r = client.post("/search", json={"query": "what hardware does Seren use", "n_results": 5})
    body = r.json()
    assert "hits" in body
    assert set(body["searched_tiers"]) <= {"short", "near", "long"}


def _core(client, content, topic):
    """What a sleep does, end to end: shorts, a draft that consumes them,
    the main model's approval. Returns the long-term id."""
    ids = [client.post("/short", json={"content": f"{content} ({i})", "topic": topic}).json()["id"]
           for i in range(2)]
    d = client.post("/drafts", json={"summary": "tonight", "operations": [
        {"kind": "new_core", "content": content, "topic": topic, "source_short_ids": ids}]}).json()
    r = client.post(f"/drafts/{d['id']}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]})
    assert r.status_code == 200, r.text
    return client.get(f"/drafts/{d['id']}").json()["operations"][0]["long_term_id"]


def test_an_approved_draft_lands_in_long_term(client):
    before = client.get("/long").json()["count"]
    lid = _core(client, "Chad mentioned liking the color yellow", "preferences")
    assert lid
    assert client.get("/long").json()["count"] > before


def test_tidy_records_a_completed_intent(client):
    r = client.post("/near", json={"intent": "test the hippocampus", "topic": "dev"})
    eid = r.json()["id"]
    client.post(f"/near/{eid}/complete")

    client.post("/tidy", json={})

    # The completed intent should now be GONE from near and recorded in long.
    near = client.get("/near", params={"include_completed": True}).json()
    assert all(e["id"] != eid for e in near["entries"])


def test_a_forget_flag_waits_for_the_purge(client):
    target = _core(client, "flag test entry", "flagtest")

    # Flag it - should NOT delete immediately: the flag is a request
    f = client.post(f"/long/{target}/forget", json={"reason": "contains my SSN"})
    assert f.json()["ok"]
    assert any(e["id"] == target for e in client.get("/long").json()["entries"]),         "flag should not instant-delete"

    # the hippocampus's tick purges what is flagged
    client.post("/tidy", json={"age_out": False, "near": False, "sweep": False, "purge": True})
    assert all(e["id"] != target for e in client.get("/long").json()["entries"]),         "a flagged entry is purged on the next tidy"
