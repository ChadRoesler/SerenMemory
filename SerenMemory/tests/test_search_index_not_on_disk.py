"""
A search made before chroma has written a collection's HNSW index to disk.

mem-search-flake, found 28 Sept 2026: about 2% of searches made right after a
core landed came back with no long-tier hits. chroma 1.x raised "Error creating
hnsw segment reader: Nothing found on disk" for the vector query AND for
.get(include=["embeddings"]), so the brute-force fallback raised too and the
search route skipped the whole tier. To the hippocampus that read as "no
existing cores", and a sleep proposed a duplicate new_core instead of an
attach.

The race is timing, so these tests force it: the long collection is wrapped so
the query and any read of the stored vectors raise the way chroma does, while
documents and metadata (plain SQLite) still read.
"""
from __future__ import annotations

import pytest

from seren_memory.config import ConsolidatorConfig, MemoryConfig


class IndexNotOnDisk:
    """Stands in for a chroma collection whose HNSW segment is not written yet."""

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def query(self, *a, **kw):
        raise RuntimeError("Error executing plan: Internal error: Error creating hnsw segment "
                           "reader: Nothing found on disk")

    def get(self, *a, include=None, **kw):
        if include and "embeddings" in include:
            raise RuntimeError("Error creating hnsw segment reader: Nothing found on disk")
        return self._real.get(*a, include=include, **kw)


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False)))


def _core(client, content, topic="color"):
    did = client.post("/drafts", json={"operations": [
        {"kind": "new_core", "content": content, "topic": topic, "evidence_count": 2}]}).json()["id"]
    out = client.post(f"/drafts/{did}/review",
                      json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()
    return out["results"][0]["long_term_id"]


def test_the_fallback_ranks_without_the_stored_vectors(client):
    near = _core(client, "Chad likes blue.")
    _core(client, "The Orin Nano is the hardware floor.", topic="hardware")
    store = client.app.state.store
    store.long = IndexNotOnDisk(store.long)

    hits = store.query("long", "chad likes blue", 5)
    assert hits, "the fallback must answer from documents when the vectors cannot be read"
    assert hits[0]["id"] == near
    assert hits[0]["metadata"]["kind"] == "core"


def test_the_search_route_still_returns_the_core(client):
    core = _core(client, "Chad likes blue.")
    store = client.app.state.store
    store.long = IndexNotOnDisk(store.long)

    r = client.post("/search", json={"query": "chad likes blue, picked the blue theme", "n_results": 5,
                                     "include_short": False, "include_near": False, "include_long": True})
    assert r.status_code == 200
    ids = [h["id"] for h in r.json()["hits"] if h["tier"] == "long"]
    assert ids == [core], "a core must not vanish from search because its index is not on disk yet"


def test_a_tier_that_cannot_answer_is_logged_not_silent(client, caplog):
    store = client.app.state.store

    class Broken:
        def count(self):
            return 1

        def query(self, *a, **kw):
            raise RuntimeError("boom")

        def get(self, *a, **kw):
            raise RuntimeError("boom")

    store.long = Broken()
    with caplog.at_level("WARNING", logger="seren_memory.search"):
        r = client.post("/search", json={"query": "anything", "include_short": False,
                                         "include_near": False, "include_long": True})
    assert r.status_code == 200 and r.json()["hits"] == []
    assert any("long tier failed" in m for m in caplog.messages)
