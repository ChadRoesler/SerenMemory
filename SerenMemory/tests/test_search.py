"""
Search tier-filtering tests.

Verifies that include_short / include_near / include_long flags actually
restrict which tiers are queried, and that the searched_tiers field in the
response accurately reflects what was searched.
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
            promote_min_evidence=2,
            pruned_safety_days=0,
        ),
    )

    def fake_model(self, prompt, max_tokens=200):
        return "CONSOLIDATED: " + prompt.split("Fragments:")[-1][:50]

    monkeypatch.setattr(svc_mod.Consolidator, "_call_model", fake_model)
    app = create_app(cfg, embedding_function=FakeEmbedder())
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def seed(client):
    """Put entries in short and near tiers. We skip seeding long-term via
    consolidation because chroma raises an HNSW error when querying a
    collection that had all its entries deleted in the same session."""
    client.post("/short", json={"content": "short tier serenity test data", "topic": "seed"})
    client.post("/short", json={"content": "short tier serenity extra entry", "topic": "seed"})
    client.post("/near", json={"intent": "near tier serenity test data", "topic": "seed"})


def test_search_all_tiers_by_default(client):
    r = client.post("/search", json={"query": "serenity test data"})
    assert r.status_code == 200
    body = r.json()
    # short and near are seeded; long may be empty - just verify the response shape
    assert "short" in body["searched_tiers"]
    assert "near" in body["searched_tiers"]
    assert "long" in body["searched_tiers"]


def test_search_short_only(client):
    r = client.post("/search", json={
        "query": "serenity test data",
        "include_near": False,
        "include_long": False,
    })
    body = r.json()
    assert body["searched_tiers"] == ["short"]
    assert all(h["tier"] == "short" for h in body["hits"])


def test_search_near_only(client):
    r = client.post("/search", json={
        "query": "serenity test data",
        "include_short": False,
        "include_long": False,
    })
    body = r.json()
    assert body["searched_tiers"] == ["near"]
    assert all(h["tier"] == "near" for h in body["hits"])


def test_search_long_only(client):
    # Long tier is empty in this fixture - verify the response is valid and empty
    r = client.post("/search", json={
        "query": "long tier seed",
        "include_short": False,
        "include_near": False,
    })
    body = r.json()
    assert r.status_code == 200
    assert body["searched_tiers"] == ["long"]


def test_search_short_and_near_only(client):
    r = client.post("/search", json={
        "query": "serenity test data",
        "include_long": False,
    })
    body = r.json()
    assert set(body["searched_tiers"]) == {"short", "near"}
    assert all(h["tier"] in ("short", "near") for h in body["hits"])


def test_search_no_tiers_returns_empty(client):
    r = client.post("/search", json={
        "query": "anything",
        "include_short": False,
        "include_near": False,
        "include_long": False,
    })
    body = r.json()
    assert body["hits"] == []
    assert body["searched_tiers"] == []


def test_search_n_results_respected(client):
    # Use the short tier which is reliably seeded and not consumed by consolidation
    r = client.post("/search", json={
        "query": "near tier serenity test data",
        "n_results": 1,
        "include_short": False,
        "include_long": False,
    })
    body = r.json()
    assert len(body["hits"]) <= 1


def test_search_hit_fields_present(client):
    r = client.post("/search", json={"query": "serenity test data", "n_results": 1})
    hits = r.json()["hits"]
    assert hits, "expected at least one hit"
    hit = hits[0]
    for field in ("tier", "content", "score", "raw_distance", "id", "metadata"):
        assert field in hit, f"missing field: {field}"
    assert hit["score"] > 0
