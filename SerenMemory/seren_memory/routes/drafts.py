"""
Draft routes - /drafts/*.

The hippocampus submits; the main model reviews per operation; the store
applies. See seren_memory.draft for the shape and the reasons.

    POST /drafts                 - submit a draft (the hippocampus)
    GET  /drafts?status=pending  - the review queue (newest first)
    GET  /drafts/{id}            - one draft with every operation's verdict
    GET  /drafts/{id}/chain      - every attempt in the draft's chain
    POST /drafts/{id}/review     - {"decisions": [{"op": 0, "verdict": "approve"},
                                                   {"op": 1, "verdict": "deny", "critique": "..."}]}
    POST /drafts/{id}/close      - the hippocampus culls a reviewed draft once its chain has landed
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ..draft import DraftError
from ..models.schemas import Draft

# Mounted twice by the app: at /drafts, and at /dockets as a deprecated
# alias so a hippocampus built before the rename keeps working until it
# is upgraded too. Drop the alias one release after both have moved.
router = APIRouter(tags=["drafts"])


@router.post("")
async def submit_draft(request: Request, draft: Draft = Body(...)):
    store = request.app.state.store
    try:
        saved = store.submit_draft(draft)
    except DraftError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": saved.id, "cluster_id": saved.cluster_id,
            "operations": len(saved.operations)}


@router.get("")
async def list_drafts(request: Request, status: Optional[str] = None, limit: int = 20):
    store = request.app.state.store
    rows = store.list_drafts(status=status, limit=limit)
    return {"count": len(rows), "entries": [d.model_dump() for d in rows]}


@router.get("/{draft_id}")
async def get_draft(request: Request, draft_id: str):
    store = request.app.state.store
    d = store.get_draft(draft_id)
    if d is None:
        raise HTTPException(404, f"no draft '{draft_id}'")
    return d.model_dump()


@router.get("/{draft_id}/chain")
async def draft_chain(request: Request, draft_id: str):
    store = request.app.state.store
    d = store.get_draft(draft_id)
    if d is None:
        raise HTTPException(404, f"no draft '{draft_id}'")
    chain = store.draft_chain(d.cluster_id or d.id)
    return {"cluster_id": d.cluster_id or d.id, "attempts": [x.model_dump() for x in chain],
            "count": len(chain)}


@router.post("/{draft_id}/review")
async def review_draft(request: Request, draft_id: str, body: dict = Body(...)):
    """Approve or deny operations, one verdict each. Approved operations are
    applied now; denied ones carry a critique back to the hippocampus.
    edited_content on an approval is accepted only on a terminal draft."""
    decisions = (body or {}).get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(400, "'decisions' must be a non-empty list of {op, verdict, critique?}")
    store = request.app.state.store
    try:
        result = store.review_draft(draft_id, decisions, note=(body or {}).get("note"))
    except KeyError:
        raise HTTPException(404, f"no draft '{draft_id}'")
    except DraftError as e:
        raise HTTPException(409 if "already" in str(e) else 400, str(e))
    return {"ok": True, **result}


@router.post("/{draft_id}/close")
async def close_draft(request: Request, draft_id: str):
    """Close a reviewed draft: the chain has landed in long-term and the
    hippocampus is culling. A pending draft still owes verdicts (409)."""
    store = request.app.state.store
    try:
        d = store.close_draft(draft_id)
    except KeyError:
        raise HTTPException(404, f"no draft '{draft_id}'")
    except DraftError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "id": d.id, "status": d.status.value}
