"""
LongTerm routes - /long/*.

THE GATED TIER. Reads are open. Direct writes are NOT exposed - long-term
is written exclusively by the consolidator. What IS exposed:

    GET  /long              - list (debugging / dashboard)
    POST /long/{id}/forget  - FLAG a memory for the consolidator to handle
                              (the Lacuna boundary: a flag, not a scalpel)

There is deliberately NO POST /long to create and NO DELETE /long/{id} to
remove. If you want to add a long-term memory, you write it to short-term
and let consolidation earn its promotion. If you want one gone, you flag it
and the consolidator decides. This is the ethos made mechanical: the system
won't hand you the scalpel.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

router = APIRouter(prefix="/long", tags=["long-term"])


@router.get("")
async def list_long(request: Request, include_superseded: bool = False):
    """List long-term memories. Hides superseded entries by default (the
    'old favorite color' case). Set include_superseded=true for history."""
    store = request.app.state.store
    rows = store.get_long_all()
    if not include_superseded:
        rows = [r for r in rows if not r["metadata"].get("superseded_by")]
    rows.sort(key=lambda r: r["metadata"].get("last_confirmed", 0), reverse=True)
    return {"count": len(rows), "entries": rows}


@router.post("/{entry_id}/forget")
async def flag_forget(request: Request, entry_id: str, body: dict = Body(...)):
    """Flag a long-term memory for the consolidator's attention. Provide a
    reason - it steers what the consolidator does:

        - PII / secrets ("contains my SSN")  → consolidator purges
        - disputed fact ("that's wrong")     → consolidator demotes/supersedes
        - no-longer-relevant                 → consolidator may let it age

    This does NOT immediately delete. The flag is recorded; the consolidator
    acts on its next run. If you need something gone RIGHT NOW for safety
    (leaked secret), that's a real gap - see the README's 'emergency purge'
    note. We don't expose instant deletion as a casual API because casual
    deletion is exactly what we're avoiding."""
    reason = (body or {}).get("reason", "").strip()
    if not reason:
        raise HTTPException(400, "a 'reason' is required to flag a memory for forgetting")
    store = request.app.state.store
    ok = store.flag_long_forget(entry_id, reason)
    if not ok:
        raise HTTPException(404, f"no long-term entry '{entry_id}'")
    return {
        "ok": True,
        "flagged": entry_id,
        "reason": reason,
        "note": "Recorded. The consolidator will act on this flag on its next "
                "run. This is intentional - memory changes are mediated, not "
                "instant.",
    }
