"""
/brief endpoint and its effect on consolidation.

Verifies that:
- a brief is accepted and stored
- promote_hints from a brief cause matching short-term entries to be promoted
  even when they wouldn't hit the default evidence threshold alone
- noise_hints suppress promotion of matching entries
"""
from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from seren_memory.app import create_app
from seren_memory.config import MemoryConfig, StorageConfig, ConsolidatorConfig
from seren_memory.consolidator import service as svc_mod


@pytest.fixture
def client(monkeypatch, fake_embedder):
    """FakeEmbedder and approve_pending_drafts live in conftest.py."""
    tmp = tempfile.mkdtemp()
    cfg = MemoryConfig(
        storage=StorageConfig(persist_dir=tmp),
        consolidator=ConsolidatorConfig(
            enabled=False,
            promote_min_evidence=3,
            pruned_safety_days=0,
        ),
    )

    def fake_model(self, prompt, max_tokens=200):
        return "CONSOLIDATED: " + prompt.split("Fragments:")[-1][:50]

    monkeypatch.setattr(svc_mod.Consolidator, "_call_model", fake_model)
    app = create_app(cfg, embedding_function=fake_embedder)
    with TestClient(app) as c:
        yield c


# ── basic brief storage ───────────────────────────────────────────────────

def test_brief_accepted(client):
    r = client.post("/brief", json={"summary": "Worked on memory consolidation today."})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "id" in body


def test_brief_with_all_fields(client):
    r = client.post("/brief", json={
        "summary": "Great session. Fixed the search ranking.",
        "completed_intents": ["fix search ranking"],
        "promote_hints": ["search ranking", "chromadb"],
        "noise_hints": ["small talk"],
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_multiple_briefs_accepted(client):
    for i in range(3):
        r = client.post("/brief", json={"summary": f"Session {i} summary."})
        assert r.json()["ok"] is True


# ── brief promote_hints steer consolidation ───────────────────────────────

def test_promote_hint_overrides_evidence_threshold(client, approve_pending_drafts):
    """A single short-term entry should clear the cluster threshold when its
    topic matches a promote_hint, even though promote_min_evidence=3, and
    then promote through the HITL gate."""
    client.post("/short", json={"content": "Chad uses NixOS on his laptop", "topic": "nixos"})

    client.post("/brief", json={
        "summary": "Discussed Chad's dev environment.",
        "promote_hints": ["nixos"],
    })

    before_long = client.get("/long").json()["count"]
    report = client.post("/consolidate/run").json()["report"]

    # The promote_hint's job is to lower the cluster threshold so a 1-entry
    # cluster gets drafted. Assert that distinct claim before we step the gate:
    assert report["drafted"] >= 1, "promote_hint should have caused a cluster draft"

    # Then approve and confirm the full pipe reaches long-term.
    approve_pending_drafts(client)
    after_long = client.get("/long").json()["count"]
    assert after_long > before_long, "approved draft should have landed in long-term"


# ── brief noise_hints suppress consolidation ──────────────────────────────

def test_noise_hint_suppresses_promotion(client):
    """Entries whose topic matches a noise_hint should NOT be promoted even
    if there are enough of them to meet the evidence threshold."""
    for i in range(4):
        client.post("/short", json={
            "content": f"Chad mentioned the weather ({i})",
            "topic": "weather",
        })

    client.post("/brief", json={
        "summary": "Lots of small talk today.",
        "noise_hints": ["weather"],
    })

    before = client.get("/long").json()["count"]
    client.post("/consolidate/run")
    after = client.get("/long").json()["count"]

    assert client.get("/drafts").json()["count"] == 0
    assert after == before, "noise_hint should have suppressed weather entries"
