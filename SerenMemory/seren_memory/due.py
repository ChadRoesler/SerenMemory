"""
seren_memory.due - what is due: the near tier read by its triggers, not by
similarity.

An intent says when it wants to come back: a time (a unix timestamp after
which it is due), an event (a match string, "mentions:balatro"), or always (a
standing note). Recall finds an intent only when a question happens to
resemble it, and "what's due?" resembles nothing in particular: on 29 Oct
2026 that question put four intents at similarity 0.39, 0.28, 0.15 and 0.14
and surfaced none of them, while the one for the next day's planned outage
sat there unread. The model does not feel time pass; this is the clock it asks.

`bucket(rows, now)` sorts open intents into:
    due       time intents whose moment has passed, most overdue first
    standing  always-on notes, newest first
    waiting   time intents not yet due (with due_in) and event intents (with
              what they wait for), soonest first
    expired   past expires_at: to complete or let go, not to act on
Completed intents are left out everywhere: they are history awaiting the
next sleep. A trigger_value that is not a timestamp (ISO dates are read;
anything else is not) lands in waiting with a note, never silently in due.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional


def _when(value: Any) -> Optional[float]:
    """A time trigger's moment as unix seconds: a number, a numeric string,
    or an ISO date / datetime. None when it cannot be read."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        pass
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _row(r: dict[str, Any], **extra: Any) -> dict[str, Any]:
    m = r.get("metadata") or {}
    return {"id": r.get("id"), "intent": r.get("content") or m.get("intent") or "", "topic": m.get("topic"),
            "trigger_type": m.get("trigger_type"), "trigger_value": m.get("trigger_value"),
            "created_at": m.get("created_at"), "expires_at": m.get("expires_at"), **extra}


def bucket(rows: list[dict[str, Any]], now: Optional[float] = None) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    due: list[dict[str, Any]] = []
    standing: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    for r in rows:
        m = r.get("metadata") or {}
        if m.get("completed"):
            continue
        exp = _when(m.get("expires_at"))
        if exp is not None and exp <= now:
            expired.append(_row(r, expired_for=round(now - exp)))
            continue
        kind = str(m.get("trigger_type") or "time")
        if kind == "always":
            standing.append(_row(r))
        elif kind == "event":
            waiting.append(_row(r, waits_for=str(m.get("trigger_value") or ""), sort=float("inf")))
        else:
            at = _when(m.get("trigger_value"))
            if at is None:
                waiting.append(_row(r, note="a time intent with no readable time: it never comes due on its own",
                                    sort=float("inf")))
            elif at <= now:
                due.append(_row(r, due_at=at, overdue=round(now - at)))
            else:
                waiting.append(_row(r, due_at=at, due_in=round(at - now), sort=at))
    due.sort(key=lambda x: x["due_at"])
    standing.sort(key=lambda x: float(x.get("created_at") or 0), reverse=True)
    waiting.sort(key=lambda x: (x.pop("sort"), float(x.get("created_at") or 0)))
    expired.sort(key=lambda x: -x["expired_for"])
    parts = []
    if due:
        parts.append(f"{len(due)} due")
    if standing:
        parts.append(f"{len(standing)} standing")
    if waiting:
        soon = next((w for w in waiting if "due_in" in w), None)
        parts.append(f"{len(waiting)} waiting" + (f" (the next in {_span(soon['due_in'])})" if soon else ""))
    if expired:
        parts.append(f"{len(expired)} expired")
    return {"now": now, "due": due, "standing": standing, "waiting": waiting, "expired": expired,
            "say": (", ".join(parts) + ".") if parts else "Nothing is due and nothing is waiting."}


def _span(seconds: float) -> str:
    s = int(seconds)
    if s < 3600:
        return f"{max(1, s // 60)} min"
    if s < 86400:
        return f"{s // 3600} h"
    return f"{s // 86400} d"
