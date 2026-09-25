"""
/brief endpoint: a brief is accepted and stored.

What a brief's promote_hints and noise_hints DO is the hippocampus's side:
they steer its draft (SerenHippocampus tests/test_brief_steers.py).
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    """FakeEmbedder lives in conftest.py."""
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(pruned_safety_days=0)))


# -- basic brief storage ---------------------------------------------------

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
