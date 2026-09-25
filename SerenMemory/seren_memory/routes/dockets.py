"""
Docket routes - /dockets/*.

The hippocampus submits; the main model reviews per operation; the store
applies. See seren_memory.docket for the shape and the reasons.

    POST /dockets                 - submit a docket (the hippocampus)
    GET  /dockets?status=pending  - the review queue (newest first)
    GET  /dockets/{id}            - one docket with every operation's verdict
    GET  /dockets/{id}/chain      - every attempt in the docket's chain
    POST /dockets/{id}/review     - {"decisions": [{"op": 0, "verdict": "approve"},
                                                   {"op": 1, "verdict": "deny", "critique": "..."}]}
    POST /dockets/{id}/close      - the hippocampus culls a reviewed docket once its chain has landed
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Body, HTTPException, Request

from ..docket import DocketError
from ..models.schemas import Docket

router = APIRouter(prefix="/dockets", tags=["dockets"])


@router.post("")
async def submit_docket(request: Request, docket: Docket = Body(...)):
    store = request.app.state.store
    try:
        saved = store.submit_docket(docket)
    except DocketError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": saved.id, "cluster_id": saved.cluster_id,
            "operations": len(saved.operations)}


@router.get("")
async def list_dockets(request: Request, status: Optional[str] = None, limit: int = 20):
    store = request.app.state.store
    rows = store.list_dockets(status=status, limit=limit)
    return {"count": len(rows), "entries": [d.model_dump() for d in rows]}


@router.get("/{docket_id}")
async def get_docket(request: Request, docket_id: str):
    store = request.app.state.store
    d = store.get_docket(docket_id)
    if d is None:
        raise HTTPException(404, f"no docket '{docket_id}'")
    return d.model_dump()


@router.get("/{docket_id}/chain")
async def docket_chain(request: Request, docket_id: str):
    store = request.app.state.store
    d = store.get_docket(docket_id)
    if d is None:
        raise HTTPException(404, f"no docket '{docket_id}'")
    chain = store.docket_chain(d.cluster_id or d.id)
    return {"cluster_id": d.cluster_id or d.id, "attempts": [x.model_dump() for x in chain],
            "count": len(chain)}


@router.post("/{docket_id}/review")
async def review_docket(request: Request, docket_id: str, body: dict = Body(...)):
    """Approve or deny operations, one verdict each. Approved operations are
    applied now; denied ones carry a critique back to the hippocampus.
    edited_content on an approval is accepted only on a terminal docket."""
    decisions = (body or {}).get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise HTTPException(400, "'decisions' must be a non-empty list of {op, verdict, critique?}")
    store = request.app.state.store
    try:
        result = store.review_docket(docket_id, decisions, note=(body or {}).get("note"))
    except KeyError:
        raise HTTPException(404, f"no docket '{docket_id}'")
    except DocketError as e:
        raise HTTPException(409 if "already" in str(e) else 400, str(e))
    return {"ok": True, **result}


@router.post("/{docket_id}/close")
async def close_docket(request: Request, docket_id: str):
    """Close a reviewed docket: the chain has landed in long-term and the
    hippocampus is culling. A pending docket still owes verdicts (409)."""
    store = request.app.state.store
    try:
        d = store.close_docket(docket_id)
    except KeyError:
        raise HTTPException(404, f"no docket '{docket_id}'")
    except DocketError as e:
        raise HTTPException(409, str(e))
    return {"ok": True, "id": d.id, "status": d.status.value}
