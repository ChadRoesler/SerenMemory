"""ShortTerm routes - /short/*. Free read/write working memory."""
from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Request

from ..models.schemas import ShortTermEntry

router = APIRouter(prefix="/short", tags=["short-term"])


@router.post("")
async def add_short(request: Request, entry: ShortTermEntry = Body(...)):
    """Write a working-memory item. Real-time, no gate."""
    store = request.app.state.store
    saved = store.add_short(entry)
    return {"ok": True, "id": saved.id, "entry": saved.model_dump()}


@router.post("/reorbit")
async def reorbit(request: Request):
    """Mark every short-term's orbit again from the cores as they are now
    (config orbit): for a store from before orbits, a store seeded shorts
    first, or a changed threshold. Answers {shorts, orbiting}."""
    store = request.app.state.store
    return {"ok": True, **store.reorbit_shorts()}


@router.get("")
async def list_short(request: Request, limit: int = 100):
    """List short-term entries (most recent first). Mostly for debugging /
    dashboard - normal recall goes through /search."""
    store = request.app.state.store
    rows = store.get_short_all(limit=None)
    rows.sort(key=lambda r: r["metadata"].get("ts", 0), reverse=True)
    return {"count": len(rows), "entries": rows[:limit]}


@router.get("/aging")
async def aging(request: Request, within_seconds: float = 0.0):
    """The short-terms that reach the end of their lifetime within
    within_seconds from now (already past it included), oldest first - what
    a sleep decides about, and the only ones it touches. The lifetime is
    Memory's (lifetimes.short_term_seconds), so the hippocampus never keeps
    a second copy of the number. Pinned ones are listed and say so; ones a
    pending draft holds are not."""
    store = request.app.state.store
    lifetime = float(request.app.state.config.lifetimes.short_term_seconds)
    rows = store.aging_short(lifetime, max(0.0, float(within_seconds)))
    return {"lifetime_seconds": lifetime, "within_seconds": within_seconds,
            "count": len(rows), "entries": rows}


@router.delete("/{entry_id}")
async def delete_short(request: Request, entry_id: str):
    """Remove a short-term entry. This is fine to expose freely - short-term
    is the scratchpad tier, deleting from it is expected (context offload
    cleanup). Not a Lacuna concern; this tier is meant to be mutable."""
    store = request.app.state.store
    store.delete_short([entry_id])
    return {"ok": True, "deleted": entry_id}
