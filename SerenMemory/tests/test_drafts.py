"""
Consolidator drafts (HITL gate) — direct lifecycle tests.

Wave 2 routed cluster synthesis through ``seren_consolidator_drafts``. The
contract this file documents:

- consolidate writes drafts; long-term stays empty until something approves
- approve: commits the draft to long-term, archives source shorts to the
  pruned tier (the "wrapped deeper" path), and removes them from short.
  Draft status -> APPROVED with a forward link to the new long-term id.
- reject: discards the draft with a reason; source shorts stay in place
  (they'll re-cluster on the next pass). Status -> REJECTED.
- both are idempotent over re-doing (already-reviewed returns 409).
- reject requires a reason (400 without one).
- missing draft id returns 404 on either action.
- ``GET /drafts?status=pending`` filters out already-reviewed entries.

These bypass tests for verbatim peel-off and completed-near live in
test_smoke.py; that's deliberate — those paths are NOT supposed to go
through this gate.
"""
from __future__ import annotations

import tempfile

import pytest
from fastapi.testclient import TestClient

from seren_memory.app import create_app
from seren_memory.config import MemoryConfig, StorageConfig, ConsolidatorConfig
from seren_memory.consolidator import service as svc_mod


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(monkeypatch, fake_embedder):
    """FakeEmbedder lives in conftest.py. promote_min_evidence=2 so a 2-entry
    cluster reliably drafts without needing brief hints.
    """
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
    app = create_app(cfg, embedding_function=fake_embedder)
    with TestClient(app) as c:
        yield c


def _make_pending_draft(client, topic: str = "drafttest", n: int = 2) -> str:
    """Write n short entries on the same topic and consolidate so a draft
    appears. Returns the draft id. Each test asks for fresh state, so we
    don't try to be clever about reusing existing drafts.
    """
    for i in range(n):
        client.post("/short", json={"content": f"{topic} fragment {i}", "topic": topic})
    report = client.post("/consolidate/run").json()["report"]
    assert report["drafted"] >= 1, "expected a draft from the cluster"
    pending = client.get("/drafts", params={"status": "pending"}).json()["entries"]
    assert pending, "expected a pending draft to be queued"
    return pending[0]["id"]


# ── approve happy path ───────────────────────────────────────────────────────


def test_approve_creates_long_archives_shorts(client):
    """Approve commits the draft to long-term, archives source shorts, and
    removes them from short. The shorts_archived count surfaces in the
    response so the caller can audit it without poking the store directly.
    """
    short_before = client.get("/short").json()["count"]
    draft_id = _make_pending_draft(client, topic="approve_happy", n=2)
    short_after_consolidate = client.get("/short").json()["count"]
    long_before = client.get("/long").json()["count"]

    # Consolidation alone must not have moved shorts or added longs.
    assert short_after_consolidate == short_before + 2
    assert long_before == 0

    r = client.post(f"/drafts/{draft_id}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["draft_id"] == draft_id
    assert body["long_term_id"], "approve response must surface the new long-term id"
    assert body["shorts_archived"] == 2

    # Long has the new entry; shorts went away (archived to pruned).
    assert client.get("/long").json()["count"] == 1
    assert client.get("/short").json()["count"] == short_before


def test_approve_sets_draft_status_and_forward_link(client):
    """After approval, the draft itself is marked APPROVED with a forward
    link to the long-term id it became. This is the audit-trail piece.
    """
    draft_id = _make_pending_draft(client, topic="audit_trail")
    approve_body = client.post(f"/drafts/{draft_id}/approve").json()
    long_id = approve_body["long_term_id"]

    # Pull the draft back via the listing (history view).
    drafts = client.get("/drafts").json()["entries"]
    approved = next((d for d in drafts if d["id"] == draft_id), None)
    assert approved is not None
    meta = approved["metadata"]
    assert meta["status"] == "approved"
    assert meta["long_term_id"] == long_id


def test_approve_accepts_optional_note(client):
    """The approve endpoint takes an optional 'note' in the body. It should
    be stored on the draft as review_note for the audit trail.
    """
    draft_id = _make_pending_draft(client, topic="with_note")
    r = client.post(f"/drafts/{draft_id}/approve", json={"note": "checked it twice"})
    assert r.status_code == 200

    drafts = client.get("/drafts").json()["entries"]
    approved = next(d for d in drafts if d["id"] == draft_id)
    assert approved["metadata"].get("review_note") == "checked it twice"


# ── reject happy path ────────────────────────────────────────────────────────


def test_reject_preserves_source_shorts(client):
    """Reject discards the synthesis but leaves the source shorts alone so
    they can re-cluster on a future pass. Long-term stays empty.
    """
    short_baseline = client.get("/short").json()["count"]
    draft_id = _make_pending_draft(client, topic="reject_keeps_shorts", n=2)

    r = client.post(f"/drafts/{draft_id}/reject", json={"reason": "noise, drop it"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["status"] == "rejected"
    assert body["reason"] == "noise, drop it"

    # Shorts untouched; long-term still empty.
    assert client.get("/short").json()["count"] == short_baseline + 2
    assert client.get("/long").json()["count"] == 0


def test_reject_sets_status_and_records_reason(client):
    """The reason given to /reject is stored as review_note in the draft's
    metadata, status flips to REJECTED. Same audit-trail discipline as
    approve, just the other path.
    """
    draft_id = _make_pending_draft(client, topic="reject_audit")
    client.post(f"/drafts/{draft_id}/reject", json={"reason": "duplicate of an earlier draft"})

    drafts = client.get("/drafts").json()["entries"]
    rejected = next(d for d in drafts if d["id"] == draft_id)
    assert rejected["metadata"]["status"] == "rejected"
    assert rejected["metadata"].get("review_note") == "duplicate of an earlier draft"


# ── error paths ──────────────────────────────────────────────────────────────


def test_approve_missing_draft_404(client):
    r = client.post("/drafts/nonexistent-id/approve")
    assert r.status_code == 404


def test_reject_missing_draft_404(client):
    r = client.post("/drafts/nonexistent-id/reject", json={"reason": "n/a"})
    assert r.status_code == 404


def test_reject_without_reason_400(client):
    """The reason field is the human signal — required, not just polite.
    Empty/whitespace reasons should be rejected the same way no-reason is.
    """
    draft_id = _make_pending_draft(client, topic="needs_reason")
    r = client.post(f"/drafts/{draft_id}/reject", json={})
    assert r.status_code == 400
    r = client.post(f"/drafts/{draft_id}/reject", json={"reason": "   "})
    assert r.status_code == 400


def test_approve_then_approve_again_409(client):
    """Reviewing twice is a no-op-with-error: 409 conflict. Protects against
    double-clicks from the viewer and replays from the MCP-tool path.
    """
    draft_id = _make_pending_draft(client, topic="double_approve")
    assert client.post(f"/drafts/{draft_id}/approve").status_code == 200
    r2 = client.post(f"/drafts/{draft_id}/approve")
    assert r2.status_code == 409


def test_approve_then_reject_409(client):
    """Approve then reject (or vice-versa) is also 409. Once a draft is
    reviewed, it's settled - no flipping the decision.
    """
    draft_id = _make_pending_draft(client, topic="flip_blocked")
    assert client.post(f"/drafts/{draft_id}/approve").status_code == 200
    r2 = client.post(f"/drafts/{draft_id}/reject", json={"reason": "wait, no"})
    assert r2.status_code == 409


def test_reject_then_reject_again_409(client):
    draft_id = _make_pending_draft(client, topic="double_reject")
    assert client.post(f"/drafts/{draft_id}/reject", json={"reason": "drop"}).status_code == 200
    r2 = client.post(f"/drafts/{draft_id}/reject", json={"reason": "drop again"})
    assert r2.status_code == 409


# ── listing / filter behaviour ───────────────────────────────────────────────


def test_pending_filter_excludes_reviewed_drafts(client):
    """``GET /drafts?status=pending`` is what the viewer's queue uses. It must
    drop drafts that have been approved or rejected.
    """
    keep_id = _make_pending_draft(client, topic="will_stay_pending")
    approve_me = _make_pending_draft(client, topic="will_be_approved")
    reject_me = _make_pending_draft(client, topic="will_be_rejected")

    client.post(f"/drafts/{approve_me}/approve")
    client.post(f"/drafts/{reject_me}/reject", json={"reason": "test"})

    pending = client.get("/drafts", params={"status": "pending"}).json()["entries"]
    pending_ids = {d["id"] for d in pending}
    assert keep_id in pending_ids
    assert approve_me not in pending_ids
    assert reject_me not in pending_ids

    # No filter -> all three visible (history view).
    all_drafts = client.get("/drafts").json()["entries"]
    all_ids = {d["id"] for d in all_drafts}
    assert {keep_id, approve_me, reject_me} <= all_ids
