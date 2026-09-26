"""
The audit: every sleep's chain, end to end, and how each model did.

Chad, 26 Sept 2026: "being able to expose briefs, corrections, and drafts...
to help catch any drift that may occur or any issues or validation if
swapping out consolidators." Everything was already kept - the brief (kept
as history once consumed), every attempt in a chain, each operation's verdict
and critique, whether the reviewer rewrote it at the terminal, what it became
in long-term - but it could only be read one draft at a time.

A chain reads brief -> attempt 1 -> critiques -> attempt 2 -> ... -> what
landed. The numbers are per model (the hippocampus stamps each draft with the
model that wrote it: model_served, what the server says it runs; model_name,
what the config asked for; model_prompt, the prompt version; model_mode,
"model" or "mechanical"), so a swap, or drift in one model, shows as numbers:

  first_pass_rate      approved / reviewed, on attempt 1 only
  mean_attempts        the attempt an operation landed on, averaged
  repeated_denials     a redraft of a denied operation denied AGAIN - the
                       critique did not take (matched on source short-terms)
  edited_on_approval   landed only because the reviewer rewrote it
  chains_ended_denied  the last permitted attempt was still denied

Read-only. Nothing here changes a draft.
"""
from __future__ import annotations

import json
from typing import Any, Optional

UNSTAMPED = "unknown (before model stamps)"


def _decode(v: Any) -> Any:
    if isinstance(v, str) and v[:1] in "[{":
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def model_of(extra: dict[str, Any]) -> dict[str, Any]:
    """The stamp as the hippocampus left it, and the key the numbers group
    by: what the server said it runs, else what the config asked for."""
    mode = str(extra.get("model_mode") or "")
    served = str(extra.get("model_served") or "")
    name = str(extra.get("model_name") or "")
    prompt = str(extra.get("model_prompt") or "")
    if mode == "mechanical":
        key = "mechanical (no model)"
    elif served or name:
        key = served or name
    else:
        key = UNSTAMPED
    if prompt and key != UNSTAMPED:
        key = f"{key} · prompt {prompt}"
    return {"key": key, "mode": mode or None, "served": served or None,
            "name": name or None, "prompt": prompt or None}


def _brief(store, brief_id: Optional[str]) -> Optional[dict[str, Any]]:
    if not brief_id:
        return None
    row = store.get_brief(brief_id)
    if row is None:
        return {"id": brief_id, "missing": True}
    meta = row.get("metadata") or {}
    return {"id": brief_id, "summary": row.get("content") or "",
            "promote_hints": _decode(meta.get("promote_hints")) or [],
            "noise_hints": _decode(meta.get("noise_hints")) or [],
            "created_at": meta.get("created_at"), "consumed_at": meta.get("consumed_at")}


def _op(op) -> dict[str, Any]:
    return {"index": op.index, "kind": op.kind.value, "content": op.content, "topic": op.topic,
            "target_core_id": op.target_core_id, "rationale": op.rationale,
            "source_short_ids": list(op.source_short_ids or []), "status": op.status.value,
            "critique": op.critique, "edited_content": op.edited_content,
            "long_term_id": op.long_term_id}


def build_audit(store, limit: int = 20, since: Optional[float] = None) -> dict[str, Any]:
    drafts = store.list_drafts(limit=1_000_000)
    if since:
        drafts = [d for d in drafts if (d.created_at or 0) >= since]
    chains: dict[str, list] = {}
    for d in drafts:
        chains.setdefault(d.cluster_id or d.id, []).append(d)
    for c in chains.values():
        c.sort(key=lambda d: d.attempt)

    stats: dict[str, dict[str, Any]] = {}

    def st(key: str) -> dict[str, Any]:
        return stats.setdefault(key, {
            "model": key, "drafts": 0, "operations": 0, "reviewed": 0, "approved": 0,
            "first_reviewed": 0, "first_approved": 0, "landed": 0, "_attempt_sum": 0,
            "edited_on_approval": 0, "redrafts_reviewed": 0, "repeated_denials": 0,
            "chains_ended_denied": 0})

    out_chains = []
    for cid, attempts in chains.items():
        prev_denied: list[set] = []            # source short-term sets denied on the previous attempt
        rendered = []
        for d in attempts:
            m = model_of(d.extra)
            s = st(m["key"])
            s["drafts"] += 1
            denied_now: list[set] = []
            for op in d.operations:
                s["operations"] += 1
                srcs = set(op.source_short_ids or [])
                decided = op.status.value in ("approved", "denied")
                follows_denial = bool(srcs) and any(srcs & p for p in prev_denied)
                if decided:
                    s["reviewed"] += 1
                    if d.attempt == 1:
                        s["first_reviewed"] += 1
                    if follows_denial:
                        s["redrafts_reviewed"] += 1
                if op.status.value == "approved":
                    s["approved"] += 1
                    s["landed"] += 1
                    s["_attempt_sum"] += d.attempt
                    if d.attempt == 1:
                        s["first_approved"] += 1
                    if op.edited_content:
                        s["edited_on_approval"] += 1
                elif op.status.value == "denied":
                    denied_now.append(srcs)
                    if follows_denial:
                        s["repeated_denials"] += 1
            prev_denied = denied_now
            rendered.append({"draft_id": d.id, "attempt": d.attempt, "terminal": d.terminal,
                             "status": d.status.value, "created_at": d.created_at,
                             "reviewed_at": d.reviewed_at, "model": m, "summary": d.summary,
                             "operations": [_op(op) for op in d.operations]})
        last = attempts[-1]
        pending = any(op.status.value == "pending" for op in last.operations)
        if last.terminal and any(op.status.value == "denied" for op in last.operations):
            st(model_of(last.extra)["key"])["chains_ended_denied"] += 1
            outcome = "ended denied"
        elif pending:
            outcome = "under review"
        elif any(op.status.value == "denied" for op in last.operations):
            outcome = "waiting for a redraft"
        else:
            outcome = "landed"
        out_chains.append({
            "cluster_id": cid, "outcome": outcome, "started_at": attempts[0].created_at,
            "brief": _brief(store, attempts[0].brief_id_used),
            "landed": sum(1 for d in attempts for op in d.operations if op.status.value == "approved"),
            "attempts": rendered})

    out_chains.sort(key=lambda c: c["started_at"] or 0, reverse=True)
    models = []
    for s in stats.values():
        s = dict(s)
        attempt_sum = s.pop("_attempt_sum")
        s["first_pass_rate"] = round(s["first_approved"] / s["first_reviewed"], 3) if s["first_reviewed"] else None
        s["mean_attempts"] = round(attempt_sum / s["landed"], 2) if s["landed"] else None
        s["repeat_rate"] = round(s["repeated_denials"] / s["redrafts_reviewed"], 3) if s["redrafts_reviewed"] else None
        models.append(s)
    models.sort(key=lambda s: s["drafts"], reverse=True)
    return {"chains": out_chains[:max(0, limit)], "chain_count": len(out_chains), "models": models}
