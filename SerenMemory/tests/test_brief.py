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
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings


class FakeEmbedder(EmbeddingFunction):
    _DIM = 64

    def __call__(self, input: Documents) -> Embeddings:
        out = []
        for text in input:
            vec = [0.0] * self._DIM
            for tok in text.lower().split():
                vec[hash(tok) % self._DIM] += 1.0
            mag = sum(v * v for v in vec) ** 0.5 or 1.0
            out.append([v / mag for v in vec])
        return out


@pytest.fixture
def client(monkeypatch):
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
    app = create_app(cfg, embedding_function=FakeEmbedder())
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

def test_promote_hint_overrides_evidence_threshold(client):
    """A single short-term entry should get promoted when its topic matches a
    promote_hint from the latest brief, even though promote_min_evidence=3."""
    client.post("/short", json={"content": "Chad uses NixOS on his laptop", "topic": "nixos"})

    client.post("/brief", json={
        "summary": "Discussed Chad's dev environment.",
        "promote_hints": ["nixos"],
    })

    before = client.get("/long").json()["count"]
    client.post("/consolidate/run")
    after = client.get("/long").json()["count"]

    assert after > before, "promote_hint should have pushed the entry to long-term"


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

    assert after == before, "noise_hint should have suppressed weather entries"
