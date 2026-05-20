"""NearTerm routes - /near/*. Open loops: future-tense intents."""
from __future__ import annotations

import time

from fastapi import APIRouter, Body, HTTPException, Request

from ..models.schemas import NearTermEntry

router = APIRouter(prefix="/near", tags=["near-term"])


@router.post("")
async def add_near(request: Request, entry: NearTermEntry = Body(...)):
    """Write an open-loop intent. Real-time, no gate - near-term is the most
    time-sensitive tier (it's about the FUTURE), so gating creation behind
    consolidation would defeat the purpose."""
    store = request.app.state.store
    saved = store.add_near(entry)
    return {"ok": True, "id": saved.id, "entry": saved.model_dump()}


@router.get("")
async def list_near(request: Request, include_completed: bool = False):
    """List open loops. By default hides completed ones (they're awaiting
    promotion to long-term by the consolidator)."""
    store = request.app.state.store
    rows = store.get_near_all()
    if not include_completed:
        rows = [r for r in rows if not r["metadata"].get("completed", False)]
    rows.sort(key=lambda r: r["metadata"].get("created_at", 0), reverse=True)
    return {"count": len(rows), "entries": rows}


@router.post("/{entry_id}/complete")
async def complete_near(request: Request, entry_id: str):
    """Mark an intent as ACTED ON (not merely referenced). The consolidator
    promotes completed intents to long-term as a record. This is a status
    flip on an entry you own, not a content edit."""
    store = request.app.state.store
    ok = store.update_near(entry_id, {
        "completed": True,
        "completed_at": time.time(),
    })
    if not ok:
        raise HTTPException(404, f"no near-term entry '{entry_id}'")
    return {"ok": True, "completed": entry_id}


@router.delete("/{entry_id}")
async def delete_near(request: Request, entry_id: str):
    """Drop an open loop (decided not to do the thing). Fine to expose -
    abandoning an intent is a normal action, not a memory-integrity concern."""
    store = request.app.state.store
    store.delete_near([entry_id])
    return {"ok": True, "deleted": entry_id}
