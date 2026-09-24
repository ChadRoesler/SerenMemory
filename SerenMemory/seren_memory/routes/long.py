"""
LongTerm routes - /long/*.

THE GATED TIER. Reads are open. Direct writes are NOT exposed - long-term
is written exclusively by the consolidator. What IS exposed:

    GET  /long                  - list cores (debugging / dashboard)
    GET  /long/{id}/satellites  - the surroundings of a core
    POST /long/{id}/forget      - FLAG a memory: a purge request, executed at
                                  the hippocampus's next sleep with a tombstone
    POST /long/{id}/purge       - execute the purge now, with the cascade
                                  (the hippocampus at sleep; the emergency door)

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
async def list_long(request: Request, include_superseded: bool = False,
                    include_satellites: bool = False):
    """List long-term memories. Hides superseded entries by default (the
    'old favorite color' case) and satellites (a core's surroundings);
    set include_superseded / include_satellites for those."""
    store = request.app.state.store
    rows = store.get_long_all()
    if not include_superseded:
        rows = [r for r in rows if not r["metadata"].get("superseded_by")]
    if not include_satellites:
        rows = [r for r in rows if r["metadata"].get("kind", "core") != "satellite"]
    rows.sort(key=lambda r: r["metadata"].get("last_confirmed", 0), reverse=True)
    return {"count": len(rows), "entries": rows}


@router.get("/{entry_id}/satellites")
async def satellites(request: Request, entry_id: str):
    """The surroundings of a core: its supporting episodes, oldest first,
    plus the core it superseded (if any). This is the docket around a hit."""
    store = request.app.state.store
    core = store.get_by_id(entry_id)
    if core is None or core.get("tier") != "long":
        raise HTTPException(404, f"no long-term entry '{entry_id}'")
    sats = store.satellites_of(entry_id)
    superseded = core["metadata"].get("supersedes")
    return {"core": core, "satellites": sats, "count": len(sats),
            "supersedes": store.get_by_id(superseded) if superseded else None}


@router.post("/{entry_id}/purge")
async def purge(request: Request, entry_id: str, body: dict = Body(...)):
    """Execute a purge now. Removes the entry, its satellites, the source
    short-terms wherever they still sit, drafts that became it, scrubs the
    docket operations that touched it, and (purge_backups, default true)
    every migration backup beside the store. Writes a tombstone that holds
    the id, the reason and what was removed - never the content.

    This is the forget flag executed. The hippocampus calls it at sleep for
    flagged entries; a person calls it through the model for the emergency
    ('I pasted you my SSH key'). Body: {"reason": "...", "purge_backups": true}
    """
    reason = (body or {}).get("reason", "").strip()
    if not reason:
        raise HTTPException(400, "a 'reason' is required to purge a memory")
    store = request.app.state.store
    tomb = store.purge_long(entry_id, reason,
                            purge_backups=bool((body or {}).get("purge_backups", True)))
    if tomb is None:
        raise HTTPException(404, f"no long-term entry '{entry_id}'")
    return {"ok": True, "tombstone": tomb}


@router.post("/{entry_id}/forget")
async def flag_forget(request: Request, entry_id: str, body: dict = Body(...)):
    """Flag a long-term memory for purging. The flag means PURGE: the
    hippocampus executes it at its next sleep with a tombstone and the
    cascade (POST /long/{id}/purge is that execution, and the emergency
    door when it cannot wait). A reason is required and is the only thing
    the tombstone keeps.

    This is not how a fact gets corrected. "I like yellow now" is a new
    memory; the docket supersedes blue with yellow and keeps blue as
    history. Flagging is for what must not exist."""
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
        "note": "Recorded. The hippocampus purges it at its next sleep, with a "
                "tombstone. For an emergency, POST /long/{id}/purge executes it now.",
    }
