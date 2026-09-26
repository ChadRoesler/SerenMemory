"""
/audit - every sleep's chain end to end, and how each model did.

    GET /audit?limit=20&since=<epoch>
        chains: brief -> attempts -> each operation's verdict, critique and
                edit -> what landed, newest first
        models: per model (as the hippocampus stamped each draft) - first
                pass rate, attempts to land, repeated denials, edits on
                approval, chains that ended denied

See seren_memory.audit for what each number means and why it exists.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request

from ..audit import build_audit

router = APIRouter(tags=["audit"])


@router.get("/audit")
async def audit(request: Request, limit: int = 20, since: Optional[float] = None):
    return build_audit(request.app.state.store, limit=limit, since=since)
