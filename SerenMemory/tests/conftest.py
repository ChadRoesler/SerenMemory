"""Shared test fixtures and helpers.

Pytest auto-discovers conftest.py, so anything declared as a fixture here is
available to every test in this directory without imports.

What's here:
  - FakeEmbedder + the `fake_embedder` fixture: deterministic offline embedder
    so tests don't need to download the all-MiniLM model or hit the network.
    The hash-based bag-of-words vector isn't semantically great, but the
    tests check the PIPELINE (write/promote/age/search-returns-something),
    not embedding quality.

  - `approve_pending_drafts` fixture: returns a callable that approves all
    pending consolidator drafts. Wave 2 put cluster synthesis behind a HITL
    gate; tests that want to observe the full short -> long path through
    cluster promotion need to step the gate explicitly. (Verbatim peel-off
    and completed-near bypass the gate by design - those tests don't need
    this helper.)
"""
from __future__ import annotations

import pytest
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings


class FakeEmbedder(EmbeddingFunction):
    """Deterministic offline embedder for tests.

    Hash-based bag-of-words: same text -> same vector, similar text ->
    somewhat similar vector. Not semantically great, but tests here check
    the pipeline (write/promote/age/search-returns-something), not embedding
    quality.

    Implements __init__ and name() explicitly because chromadb >= 0.5 emits
    a DeprecationWarning if either is missing, and they're slated to become
    hard requirements in a future version.
    """
    _DIM = 64

    def __init__(self) -> None:
        # No config needed for the test embedder. Defined explicitly to
        # satisfy chromadb's EmbeddingFunction interface (see class docstring).
        pass

    @classmethod
    def name(cls) -> str:
        # chromadb's embedding-function registry calls name() as a
        # classmethod for type identification. Marking it as one keeps the
        # instance-side call (`self.name()`) working too (Python lets you
        # call classmethods on instances) while satisfying the registry.
        return "fake-bow-test"

    def get_config(self) -> dict:
        # chromadb >= 0.5 also requires get_config() for serializable
        # configuration. For the test embedder there's nothing to surface
        # beyond the vector dimension; an empty dict would also be fine.
        return {"dim": self._DIM}

    @classmethod
    def build_from_config(cls, config: dict) -> "FakeEmbedder":
        # Chroma calls this when reconstructing the embedder from persisted
        # config. We have no state to restore, so just return a fresh one.
        return cls()

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


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    """Fresh FakeEmbedder per test (function scope is the pytest default)."""
    return FakeEmbedder()


@pytest.fixture
def approve_pending_drafts():
    """Returns a callable: approve_pending_drafts(client) -> int.

    Approves every pending consolidator draft via the same /drafts/{id}/approve
    endpoint the Halls viewer's button calls. Wave 2 cluster synthesis writes
    drafts; this is the explicit gate-step a test takes when it wants to
    observe the resulting long-term entry. Returns the count approved.
    """
    def _approve(client) -> int:
        pending = client.get("/drafts", params={"status": "pending"}).json()["entries"]
        for d in pending:
            r = client.post(f"/drafts/{d['id']}/approve")
            assert r.status_code == 200, f"approve failed: {r.status_code} {r.text}"
        return len(pending)
    return _approve
