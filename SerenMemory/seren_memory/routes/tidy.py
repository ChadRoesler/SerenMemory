"""
/tidy - the mechanical steps of a sleep, offered over the API.

The hippocampus holds no chroma client; it asks the store to do the
housekeeping the in-process consolidator used to do in its own thread:

    age_out   short-term entries past lifetimes.short_term_seconds that are
              not pinned: archived to pruned, then removed
    near      completed intents become a long-term record; expired ones drop
    sweep     pruned entries past consolidator.pruned_safety_days are deleted
    purge     every long-term entry carrying a forget flag is purged with the
              cascade and a tombstone

Each is opt-in per call so a caller can run one without the rest. Nothing
here drafts or judges - that is the hippocampus's side of the line.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request

router = APIRouter(tags=["tidy"])


@router.post("/tidy")
async def tidy(request: Request, body: dict = Body(default={})):
    cfg = request.app.state.config
    store = request.app.state.store
    body = body or {}
    report: dict = {}
    if body.get("age_out", True):
        report["aged_out"] = store.age_out_short(
            cfg.lifetimes.short_term_seconds,
            keep_pruned=cfg.consolidator.pruned_safety_days > 0)
    if body.get("near", True):
        report["near"] = store.maintain_near()
    if body.get("sweep", True):
        report["pruned_swept"] = store.sweep_pruned(cfg.consolidator.pruned_safety_days * 24 * 3600)
    if body.get("purge", False):
        tombs = []
        for row in store.flagged_long():
            t = store.purge_long(row["id"], str(row["metadata"].get("forget_flag")),
                                 purge_backups=bool(body.get("purge_backups", True)))
            if t:
                tombs.append(t)
        report["purged"] = tombs
    return {"ok": True, **report}
