"""
Consolidator drafts (model review queue) - direct lifecycle tests.

Wave 2 routed cluster synthesis through ``seren_consolidator_drafts``. The
contract this file documents:

- consolidate writes drafts; long-term stays empty until the model approves
- approve: commits the draft to long-term, archives source shorts to the
  pruned tier (the "wrapped deeper" path), and removes them from short.
  Draft status -> APPROVED with a forward link to the new long-term id.
- reject: stores the critique, triggers a redraft from the consolidator,
  increments attempt count. Shorts stay in place for the next synthesis.
  Status -> REJECTED; new PENDING draft appears in the chain.
- After max_redraft_attempts rejections, the chain flips to
  requires_selection - the model must use GET /drafts/{id}/chain to compare
  all attempts, then POST /drafts/{id}/select to commit the best one.
- both approve and reject are idempotent over re-doing (returns 409).
- reject requires a critique (400 without one).
- missing draft id returns 404 on either action.
- ``GET /drafts?status=pending`` filters out already-reviewed entries.

Verbatim peel-off and completed-near tests live in test_smoke.py - those
paths bypass the draft queue intentionally.
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig
from seren_memory.consolidator import service as svc_mod


# ── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(make_client, monkeypatch):
    """FakeEmbedder lives in conftest.py. promote_min_evidence=2 so a 2-entry
    cluster reliably drafts without needing brief hints.
    max_redraft_attempts=2 keeps redraft-loop tests fast.
    """
    def fake_model(self, prompt, max_tokens=200):
        return "CONSOLIDATED: " + prompt.split("Fragments:")[-1][:50]

    monkeypatch.setattr(svc_mod.Consolidator, "_call_model", fake_model)
    return make_client(MemoryConfig(
        consolidator=ConsolidatorConfig(
            enabled=False,
            promote_min_evidence=2,
            pruned_safety_days=0,
            max_redraft_attempts=2,
        ),
    ))


def _make_pending_draft(client, topic: str = "drafttest", n: int = 2) -> str:
    """Write n short entries on the same topic and consolidate so a draft
    appears. Returns the draft id.
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
    response for audit.
    """
    short_before = client.get("/short").json()["count"]
    draft_id = _make_pending_draft(client, topic="approve_happy", n=2)
    short_after_consolidate = client.get("/short").json()["count"]
    long_before = client.get("/long").json()["count"]

    assert short_after_consolidate == short_before + 2
    assert long_before == 0

    r = client.post(f"/drafts/{draft_id}/approve")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["draft_id"] == draft_id
    assert body["long_term_id"], "approve must surface the new long-term id"
    assert body["shorts_archived"] == 2

    assert client.get("/long").json()["count"] == 1
    assert client.get("/short").json()["count"] == short_before


def test_approve_sets_draft_status_and_forward_link(client):
    """After approval, draft is marked APPROVED with a forward link to the
    long-term id - the audit-trail piece.
    """
    draft_id = _make_pending_draft(client, topic="audit_trail")
    approve_body = client.post(f"/drafts/{draft_id}/approve").json()
    long_id = approve_body["long_term_id"]

    drafts = client.get("/drafts").json()["entries"]
    approved = next((d for d in drafts if d["id"] == draft_id), None)
    assert approved is not None
    meta = approved["metadata"]
    assert meta["status"] == "approved"
    assert meta["long_term_id"] == long_id


def test_approve_accepts_optional_note(client):
    """Optional 'note' in the approve body is stored on the draft as
    review_note for the audit trail.
    """
    draft_id = _make_pending_draft(client, topic="with_note")
    r = client.post(f"/drafts/{draft_id}/approve", json={"note": "checked it twice"})
    assert r.status_code == 200

    drafts = client.get("/drafts").json()["entries"]
    approved = next(d for d in drafts if d["id"] == draft_id)
    assert approved["metadata"].get("review_note") == "checked it twice"


# ── reject / redraft happy path ───────────────────────────────────────────────


def test_reject_triggers_redraft_and_preserves_shorts(client):
    """Reject with a critique must: mark the draft rejected, trigger a new
    synthesis, and leave source shorts in place (they feed the redraft).
    The response action should be 'redrafted' and a new draft_id appears.
    Long-term stays empty.
    """
    short_before = client.get("/short").json()["count"]
    draft_id = _make_pending_draft(client, topic="reject_redraft", n=2)

    r = client.post(f"/drafts/{draft_id}/reject",
                    json={"critique": "too vague, add specifics"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "redrafted"
    assert body["new_draft_id"] is not None
    assert body["new_draft_id"] != draft_id
    assert body["attempt"] == 2

    # Source shorts untouched; long-term still empty.
    assert client.get("/short").json()["count"] == short_before + 2
    assert client.get("/long").json()["count"] == 0

    # The new draft is pending.
    new_draft = next(
        (d for d in client.get("/drafts").json()["entries"]
         if d["id"] == body["new_draft_id"]), None)
    assert new_draft is not None
    assert new_draft["metadata"]["status"] == "pending"
    assert new_draft["metadata"]["attempt"] == 2


def test_reject_stores_critique_on_draft(client):
    """The critique is persisted on the rejected draft's metadata."""
    draft_id = _make_pending_draft(client, topic="critique_stored")
    client.post(f"/drafts/{draft_id}/reject",
                json={"critique": "missing key context"})

    drafts = client.get("/drafts").json()["entries"]
    rejected = next(d for d in drafts if d["id"] == draft_id)
    assert rejected["metadata"]["status"] == "rejected"
    assert rejected["metadata"].get("critique") == "missing key context"


def test_reject_accepts_legacy_reason_key(client):
    """'reason' key is accepted as an alias for 'critique' for backwards
    compatibility with older callers.
    """
    draft_id = _make_pending_draft(client, topic="legacy_reason")
    r = client.post(f"/drafts/{draft_id}/reject",
                    json={"reason": "old-style rejection"})
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── redraft chain / requires_selection ───────────────────────────────────────


def test_exhausting_redrafts_flips_to_requires_selection(client):
    """When max_redraft_attempts (2 in the test fixture) are exhausted the
    action becomes 'requires_selection'. No further redraft is produced.
    """
    # Attempt 1 (initial draft created by consolidation).
    draft_id = _make_pending_draft(client, topic="exhaust_chain", n=2)

    # Rejection 1 → attempt 2 produced (redrafted).
    r1 = client.post(f"/drafts/{draft_id}/reject",
                     json={"critique": "first critique"})
    assert r1.json()["action"] == "redrafted"
    draft_id_2 = r1.json()["new_draft_id"]

    # Rejection 2 → limit reached → requires_selection.
    r2 = client.post(f"/drafts/{draft_id_2}/reject",
                     json={"critique": "second critique"})
    assert r2.status_code == 200
    body = r2.json()
    assert body["action"] == "requires_selection"
    assert body["new_draft_id"] is None


def test_chain_endpoint_returns_all_attempts(client):
    """GET /drafts/{id}/chain returns every attempt for the cluster in order."""
    draft_id = _make_pending_draft(client, topic="chain_view", n=2)
    r1 = client.post(f"/drafts/{draft_id}/reject",
                     json={"critique": "needs more detail"})
    draft_id_2 = r1.json()["new_draft_id"]

    # Chain is accessible from either draft in the cluster.
    chain = client.get(f"/drafts/{draft_id}/chain").json()
    assert chain["count"] == 2
    attempts = chain["attempts"]
    assert attempts[0]["metadata"]["attempt"] == 1
    assert attempts[1]["metadata"]["attempt"] == 2

    # Same cluster visible from the second draft.
    chain2 = client.get(f"/drafts/{draft_id_2}/chain").json()
    assert chain2["cluster_id"] == chain["cluster_id"]


def test_select_commits_best_attempt_to_long_term(client):
    """After requires_selection, POST /drafts/{id}/select commits one attempt
    to long-term and archives the source shorts.
    """
    short_before = client.get("/short").json()["count"]
    draft_id = _make_pending_draft(client, topic="select_best", n=2)

    # Exhaust redraft budget (max=2).
    r1 = client.post(f"/drafts/{draft_id}/reject",
                     json={"critique": "c1"})
    d2 = r1.json()["new_draft_id"]
    client.post(f"/drafts/{d2}/reject", json={"critique": "c2"})

    # Now attempt 1 is in requires_selection - select it.
    r = client.post(f"/drafts/{draft_id}/select")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["long_term_id"]
    assert body["shorts_archived"] == 2

    assert client.get("/long").json()["count"] == 1
    assert client.get("/short").json()["count"] == short_before


def test_select_on_pending_draft_returns_409(client):
    """Calling /select on a still-pending draft (not requires_selection)
    should return 409 - the model should approve or reject it instead.
    """
    draft_id = _make_pending_draft(client, topic="select_wrong_state")
    r = client.post(f"/drafts/{draft_id}/select")
    assert r.status_code == 409


# ── edit-on-select ───────────────────────────────────────────────────────────


def _exhaust_to_requires_selection(client, topic: str = "edit_chain",
                                    n: int = 2) -> str:
    """Helper: write n shorts, draft, exhaust the redraft budget so the
    chain flips to requires_selection. Returns the first draft's id (which
    is the cluster_id and is itself in requires_selection state)."""
    draft_id = _make_pending_draft(client, topic=topic, n=n)
    r1 = client.post(f"/drafts/{draft_id}/reject", json={"critique": "c1"})
    d2 = r1.json()["new_draft_id"]
    client.post(f"/drafts/{d2}/reject", json={"critique": "c2"})
    return draft_id


def test_select_with_edited_content_commits_edit(client):
    """When edited_content is provided on select, the long-term entry uses
    the edited text - NOT the original draft synthesis. The response
    surfaces edited=True and a non-zero edit_delta_chars so the operator
    can see at a glance that a revision happened.
    """
    draft_id = _exhaust_to_requires_selection(client, topic="edit_commits")
    edited = "the editor's polished version, longer than the draft was"

    r = client.post(f"/drafts/{draft_id}/select",
                    json={"edited_content": edited})
    assert r.status_code == 200
    body = r.json()
    assert body["edited"] is True
    assert body["edit_delta_chars"] > 0

    # Long-term entry has the edited text, not the draft's synthesis.
    long_rows = client.get("/long").json()["entries"]
    long_entry = next(r for r in long_rows if r["id"] == body["long_term_id"])
    assert long_entry["content"] == edited


def test_select_without_edit_commits_draft_as_is(client):
    """Omitting edited_content (the default path) commits the draft's own
    content to long-term unchanged. edited=False and edit_delta_chars=0
    in the response - backstops 'did anything get tweaked?' at a glance.
    """
    draft_id = _exhaust_to_requires_selection(client, topic="edit_skipped")
    # Capture what the draft's content is before commit so we can verify
    # long-term landed with exactly that text.
    chain = client.get(f"/drafts/{draft_id}/chain").json()
    draft_content = next(a["content"] for a in chain["attempts"]
                         if a["id"] == draft_id)

    r = client.post(f"/drafts/{draft_id}/select", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["edited"] is False
    assert body["edit_delta_chars"] == 0

    long_rows = client.get("/long").json()["entries"]
    long_entry = next(r for r in long_rows if r["id"] == body["long_term_id"])
    assert long_entry["content"] == draft_content


def test_select_with_blank_edit_400(client):
    """edited_content='' or whitespace-only is a bug, not 'no edit'. The
    route rejects with 400 rather than silently committing a blank
    long-term entry or treating it as omitted (which would mask the bug).
    """
    draft_id = _exhaust_to_requires_selection(client, topic="edit_blank")

    r1 = client.post(f"/drafts/{draft_id}/select",
                     json={"edited_content": ""})
    assert r1.status_code == 400

    r2 = client.post(f"/drafts/{draft_id}/select",
                     json={"edited_content": "   \t\n  "})
    assert r2.status_code == 400

    # Draft should still be selectable (the 400s left it untouched).
    r3 = client.post(f"/drafts/{draft_id}/select", json={})
    assert r3.status_code == 200


def test_select_preserves_original_in_draft_for_audit(client):
    """After select-with-edit, the draft row itself keeps the ORIGINAL
    synthesis as its content; the edited text is stored separately in
    metadata.edited_content. Audit trail intact - we can always answer
    'what did the consolidator originally synthesize for this cluster.'
    """
    draft_id = _exhaust_to_requires_selection(client, topic="edit_audit")
    # Snapshot original content before edit.
    chain_before = client.get(f"/drafts/{draft_id}/chain").json()
    original = next(a["content"] for a in chain_before["attempts"]
                    if a["id"] == draft_id)
    edited = "completely different editor version"

    r = client.post(f"/drafts/{draft_id}/select",
                    json={"edited_content": edited})
    assert r.status_code == 200

    # Pull the draft back via the chain - its content is the original,
    # and its metadata carries the edit.
    chain_after = client.get(f"/drafts/{draft_id}/chain").json()
    selected = next(a for a in chain_after["attempts"] if a["id"] == draft_id)
    assert selected["content"] == original
    assert selected["metadata"]["edited_content"] == edited
    assert selected["metadata"]["edit_delta_chars"] == abs(len(edited) - len(original))
    assert selected["metadata"]["status"] == "approved"


# ── error paths ──────────────────────────────────────────────────────────────


def test_approve_missing_draft_404(client):
    r = client.post("/drafts/nonexistent-id/approve")
    assert r.status_code == 404


def test_reject_missing_draft_404(client):
    r = client.post("/drafts/nonexistent-id/reject",
                    json={"critique": "n/a"})
    assert r.status_code == 404


def test_select_missing_draft_404(client):
    r = client.post("/drafts/nonexistent-id/select")
    assert r.status_code == 404


def test_reject_without_critique_400(client):
    """The critique field is required - empty or whitespace must return 400."""
    draft_id = _make_pending_draft(client, topic="needs_critique")
    assert client.post(f"/drafts/{draft_id}/reject", json={}).status_code == 400
    assert client.post(f"/drafts/{draft_id}/reject",
                       json={"critique": "   "}).status_code == 400


def test_approve_then_approve_again_409(client):
    """Reviewing twice returns 409. Protects against double-clicks and
    replays from the tool path.
    """
    draft_id = _make_pending_draft(client, topic="double_approve")
    assert client.post(f"/drafts/{draft_id}/approve").status_code == 200
    assert client.post(f"/drafts/{draft_id}/approve").status_code == 409


def test_approve_then_reject_409(client):
    """Approve then reject is also 409 - decision is final."""
    draft_id = _make_pending_draft(client, topic="flip_blocked")
    assert client.post(f"/drafts/{draft_id}/approve").status_code == 200
    assert client.post(f"/drafts/{draft_id}/reject",
                       json={"critique": "wait, no"}).status_code == 409


def test_reject_then_reject_again_409(client):
    """Rejecting an already-rejected draft returns 409."""
    draft_id = _make_pending_draft(client, topic="double_reject")
    assert client.post(f"/drafts/{draft_id}/reject",
                       json={"critique": "drop"}).status_code == 200
    # The original draft is now rejected; re-rejecting should 409.
    assert client.post(f"/drafts/{draft_id}/reject",
                       json={"critique": "drop again"}).status_code == 409


# ── listing / filter behaviour ───────────────────────────────────────────────


def test_pending_filter_excludes_reviewed_drafts(client):
    """``GET /drafts?status=pending`` is the model's active review queue -
    it must exclude approved and rejected drafts.
    """
    keep_id = _make_pending_draft(client, topic="will_stay_pending")
    approve_me = _make_pending_draft(client, topic="will_be_approved")
    reject_me = _make_pending_draft(client, topic="will_be_rejected")

    client.post(f"/drafts/{approve_me}/approve")
    client.post(f"/drafts/{reject_me}/reject", json={"critique": "test"})

    pending = client.get("/drafts", params={"status": "pending"}).json()["entries"]
    pending_ids = {d["id"] for d in pending}
    assert keep_id in pending_ids
    assert approve_me not in pending_ids
    assert reject_me not in pending_ids

    # No filter -> all visible (history view).
    all_ids = {d["id"] for d in client.get("/drafts").json()["entries"]}
    assert {keep_id, approve_me, reject_me} <= all_ids

