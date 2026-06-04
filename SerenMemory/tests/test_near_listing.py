"""
near-term include_completed listing tests.

Verifies that:
- completed entries are hidden by default
- include_completed=True surfaces them
- abandoned (deleted) entries are gone entirely
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(
        consolidator=ConsolidatorConfig(enabled=False),
    ))


def test_completed_hidden_by_default(client):
    r = client.post("/near", json={"intent": "complete me", "topic": "test"})
    eid = r.json()["id"]
    client.post(f"/near/{eid}/complete")

    listing = client.get("/near").json()
    assert all(e["id"] != eid for e in listing["entries"]), \
        "completed entry should be hidden from default listing"


def test_completed_visible_with_flag(client):
    r = client.post("/near", json={"intent": "complete me too", "topic": "test"})
    eid = r.json()["id"]
    client.post(f"/near/{eid}/complete")

    listing = client.get("/near", params={"include_completed": True}).json()
    ids = [e["id"] for e in listing["entries"]]
    assert eid in ids, "completed entry should appear when include_completed=True"


def test_completed_entry_has_completed_flag(client):
    r = client.post("/near", json={"intent": "check completed flag", "topic": "test"})
    eid = r.json()["id"]
    client.post(f"/near/{eid}/complete")

    listing = client.get("/near", params={"include_completed": True}).json()
    entry = next(e for e in listing["entries"] if e["id"] == eid)
    assert entry["metadata"]["completed"] is True


def test_open_entries_always_visible(client):
    r = client.post("/near", json={"intent": "still open", "topic": "test"})
    eid = r.json()["id"]

    for params in ({}, {"include_completed": True}, {"include_completed": False}):
        listing = client.get("/near", params=params).json()
        ids = [e["id"] for e in listing["entries"]]
        assert eid in ids, f"open entry should always be visible (params={params})"


def test_deleted_near_entry_gone_everywhere(client):
    r = client.post("/near", json={"intent": "abandon me", "topic": "test"})
    eid = r.json()["id"]
    client.delete(f"/near/{eid}")

    for params in ({}, {"include_completed": True}):
        listing = client.get("/near", params=params).json()
        assert all(e["id"] != eid for e in listing["entries"]), \
            f"deleted entry should never appear (params={params})"


def test_mixed_listing(client):
    open_r = client.post("/near", json={"intent": "open loop", "topic": "mixed"})
    done_r = client.post("/near", json={"intent": "done loop", "topic": "mixed"})
    open_id = open_r.json()["id"]
    done_id = done_r.json()["id"]
    client.post(f"/near/{done_id}/complete")

    default = client.get("/near").json()
    default_ids = [e["id"] for e in default["entries"]]
    assert open_id in default_ids
    assert done_id not in default_ids

    full = client.get("/near", params={"include_completed": True}).json()
    full_ids = [e["id"] for e in full["entries"]]
    assert open_id in full_ids
    assert done_id in full_ids
