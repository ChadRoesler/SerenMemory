"""
Smoke test for SerenMemory.

Boots the app with an isolated temp persist dir, exercises the full loop:
write short → write near → search → submit brief → consolidate → verify.

The consolidator's model call is monkeypatched so the test doesn't need a
live LLM - we verify the MECHANICAL pipeline (clustering, promotion, aging,
near-term maintenance), which is the part that can break silently. The model
synthesis quality is a separate (manual) concern.

Run:  pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seren_memory.app import create_app
from seren_memory.config import MemoryConfig, StorageConfig, ConsolidatorConfig


@pytest.fixture
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    cfg = MemoryConfig(
        storage=StorageConfig(persist_dir=tmp),
        consolidator=ConsolidatorConfig(
            enabled=False,           # we trigger manually, no background loop
            promote_min_evidence=2,  # lower bar for the test
            pruned_safety_days=0,    # delete immediately, simpler assertions
        ),
    )

    # Deterministic offline embedder so tests don't need to download the
    # all-MiniLM model (and don't need network at all). A hash-based
    # bag-of-words vector: same text → same vector, similar text → somewhat
    # similar vector. Not semantically great, but the smoke test checks the
    # PIPELINE (write/promote/age/search-returns-something), not embedding
    # quality. Real deployments use chroma's default model.
    from chromadb.api.types import Documents, EmbeddingFunction, Embeddings

    class FakeEmbedder(EmbeddingFunction):
        _DIM = 64

        def __call__(self, input: Documents) -> Embeddings:
            out = []
            for text in input:
                vec = [0.0] * self._DIM
                for tok in text.lower().split():
                    vec[hash(tok) % self._DIM] += 1.0
                # normalize so cosine distance behaves
                mag = sum(v * v for v in vec) ** 0.5 or 1.0
                out.append([v / mag for v in vec])
            return out

    # Monkeypatch the model call so synthesis returns a deterministic string
    # without needing a live LLM endpoint.
    from seren_memory.consolidator import service as svc_mod

    def fake_model(self, prompt, max_tokens=200):
        return "CONSOLIDATED: " + prompt.split("Fragments:")[-1][:50]

    monkeypatch.setattr(svc_mod.Consolidator, "_call_model", fake_model)

    app = create_app(cfg, embedding_function=FakeEmbedder())
    with TestClient(app) as c:
        yield c
    # temp dir leaks are fine in test - OS cleans /tmp


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


def test_consolidation_promotes_cluster(client):
    # Write 3 entries on the same topic - should cluster + promote.
    for i in range(3):
        client.post("/short", json={
            "content": f"Chad mentioned liking the color yellow ({i})",
            "topic": "preferences",
        })

    before = client.get("/long").json()["count"]
    report = client.post("/consolidate/run").json()["report"]
    after = client.get("/long").json()["count"]

    assert report["promoted"] >= 1
    assert after > before


def test_consolidation_promotes_completed_near(client):
    r = client.post("/near", json={"intent": "test the consolidator", "topic": "dev"})
    eid = r.json()["id"]
    client.post(f"/near/{eid}/complete")

    report = client.post("/consolidate/run").json()["report"]
    assert report["near_completed_promoted"] >= 1

    # The completed intent should now be GONE from near and recorded in long.
    near = client.get("/near", params={"include_completed": True}).json()
    assert all(e["id"] != eid for e in near["entries"])


def test_forget_flag_does_not_instantly_delete(client):
    # Promote something to long-term first
    for i in range(2):
        client.post("/short", json={"content": f"flag test entry {i}", "topic": "flagtest"})
    client.post("/consolidate/run")

    longs = client.get("/long").json()["entries"]
    assert longs, "expected a promoted long-term entry"
    target = longs[0]["id"]

    # Flag it - should NOT delete immediately
    f = client.post(f"/long/{target}/forget", json={"reason": "test disagreement"})
    assert f.json()["ok"]
    still_there = client.get("/long").json()["entries"]
    assert any(e["id"] == target for e in still_there), "flag should not instant-delete"

    # After consolidation, non-PII flag demotes (evidence → 0), keeps content
    client.post("/consolidate/run")
    after = client.get("/long").json()["entries"]
    demoted = next((e for e in after if e["id"] == target), None)
    assert demoted is not None
    assert demoted["metadata"].get("evidence_count") == 0


def test_pii_flag_purges(client):
    for i in range(2):
        client.post("/short", json={"content": f"pii test {i}", "topic": "piitest"})
    client.post("/consolidate/run")
    longs = client.get("/long").json()["entries"]
    target = longs[0]["id"]

    client.post(f"/long/{target}/forget", json={"reason": "contains my SSN"})
    client.post("/consolidate/run")

    after = client.get("/long").json()["entries"]
    assert all(e["id"] != target for e in after), "PII flag should purge on consolidation"
