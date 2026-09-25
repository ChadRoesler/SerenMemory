"""
seren_memory.docket
════════════════════════════════════════════════════════════════════════

The docket: what a consolidation pass proposes, and how the store applies
what the reviewer approves.

THE SHAPE (settled with Chad, 23 Sept 2026)

    A draft is not one synthesis that becomes one long-term entry. A draft
    is a DOCKET: a list of operations on long-term, each reviewed on its
    own by the main model, each carrying its own verdict and critique.

        new_core    a durable statement with the lesson in it
        attach      tonight's short-terms are evidence for a core that
                    already exists; the core's evidence grows, its wording
                    may be restated
        supersede   yellow arrives; blue becomes a demoted satellite of
                    yellow's story, still recallable through history
        verbatim    "this deserves its own thing and must not be muddled" -
                    one episode, kept word for word as its own core

    Long-term is a core and its surroundings. Recall returns cores.
    Satellites (kind=satellite, core_id=<core>) are the supporting episodes
    with their dates; a superseded core keeps superseded_by pointing forward.

WHO DOES WHAT

    The hippocampus (the small model, the Inside Out workers) writes the
    docket from the short-terms the brief steered it to, POST /dockets.
    The main model reviews it through Memory's MCP tools or POST
    /dockets/{id}/review, approving or denying PER OPERATION with a
    critique. THIS module applies approved operations, so the store stays
    consistent and the worker stays stateless. Denied operations go back
    to the hippocampus's tend loop, which resubmits a new docket in the
    same chain (cluster_id, attempt, previous_docket_ids). The last
    permitted attempt is submitted with terminal=true, and only then may
    an approval carry edited_content - the editor's release valve, never
    the loop's shortcut.

FORGET IS NOT HERE. A purge is a separate path (MemoryStore.purge_long):
    a person's flag, executed with a tombstone and a cascade. Nothing in a
    docket deletes.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from .models.schemas import (
    Docket, DocketOperation, DocketOpKind, DocketStatus, LongTermEntry, OpStatus, Source,
)


class DocketError(ValueError):
    """A review that cannot be applied: bad verdict, unknown op, wrong state."""


def _maybe_json(v: Any) -> Any:
    from .collections import _maybe_json as f
    return f(v)


def _clean_meta(d: dict[str, Any]) -> dict[str, Any]:
    from .collections import _clean_meta as f
    return f(d)


def _zip_get(res: dict[str, Any], limit: Optional[int]) -> list[dict[str, Any]]:
    from .collections import _zip_get as f
    return f(res, limit)


# ── persistence shape ────────────────────────────────────────────────────────
# One chroma document per docket. The document text is the docket's summary
# (what the embedder sees, what a viewer shows); the operations ride in
# metadata as JSON, which is the only place chroma lets a list live.

def _docket_meta(d: Docket) -> dict[str, Any]:
    return _clean_meta({
        "status": d.status.value,
        "cluster_id": d.cluster_id or d.id,
        "attempt": d.attempt,
        "terminal": d.terminal,
        "previous_docket_ids": d.previous_docket_ids,
        "brief_id_used": d.brief_id_used,
        "created_at": d.created_at,
        "reviewed_at": d.reviewed_at,
        "source": d.source.value,
        "operations": [op.model_dump() for op in d.operations],
        **d.extra,
    })


def _row_to_docket(row: dict[str, Any]) -> Docket:
    meta = dict(row["metadata"])
    ops_raw = meta.pop("operations", []) or []
    if isinstance(ops_raw, str):
        ops_raw = _maybe_json(ops_raw) or []
    prev = meta.pop("previous_docket_ids", []) or []
    if isinstance(prev, str):
        prev = _maybe_json(prev) or []
    known = {"status", "cluster_id", "attempt", "terminal", "brief_id_used",
             "created_at", "reviewed_at", "source"}
    extra = {k: v for k, v in meta.items() if k not in known}
    return Docket(
        id=row["id"],
        summary=row["content"],
        status=DocketStatus(meta.get("status", "pending")),
        cluster_id=meta.get("cluster_id") or row["id"],
        attempt=int(meta.get("attempt", 1) or 1),
        terminal=bool(meta.get("terminal", False)),
        previous_docket_ids=list(prev),
        brief_id_used=meta.get("brief_id_used"),
        created_at=float(meta.get("created_at", 0) or 0),
        reviewed_at=meta.get("reviewed_at"),
        source=Source(meta.get("source", Source.CONSOLIDATOR.value)),
        operations=[DocketOperation(**op) for op in ops_raw],
        extra=extra,
    )


class DocketMixin:
    """Mixed into MemoryStore. Needs self.dockets, self.long, self.short,
    self.pruned, self.add_long, self.archive_pruned, self.delete_short,
    self.supersede_long, self.get_by_id."""

    # -- submit / read ---------------------------------------------------------
    def submit_docket(self, d: Docket) -> Docket:
        if not d.operations:
            raise DocketError("a docket needs at least one operation")
        for i, op in enumerate(d.operations):
            op.index = i
            if op.kind in (DocketOpKind.ATTACH, DocketOpKind.SUPERSEDE) and not op.target_core_id:
                raise DocketError(f"operation {i} ({op.kind.value}) needs target_core_id")
            if op.kind in (DocketOpKind.ATTACH, DocketOpKind.SUPERSEDE):
                target = self._long_row(op.target_core_id)
                if target is None:
                    raise DocketError(f"operation {i}: no long-term entry '{op.target_core_id}'")
                if target["metadata"].get("kind", "core") != "core":
                    raise DocketError(f"operation {i}: '{op.target_core_id}' is a satellite, not a core")
            if not (op.content or "").strip() and op.kind != DocketOpKind.ATTACH:
                raise DocketError(f"operation {i} ({op.kind.value}) needs content")
        d.cluster_id = d.cluster_id or d.id
        self.dockets.add(documents=[d.summary or "(no summary)"],
                         metadatas=[_docket_meta(d)], ids=[d.id])
        return d

    def get_docket(self, docket_id: str) -> Optional[Docket]:
        rows = _zip_get(self.dockets.get(ids=[docket_id], include=["documents", "metadatas"]), None)
        return _row_to_docket(rows[0]) if rows else None

    def list_dockets(self, status: Optional[str] = None, limit: int = 20) -> list[Docket]:
        rows = _zip_get(self.dockets.get(include=["documents", "metadatas"]), None)
        out = [_row_to_docket(r) for r in rows]
        if status:
            out = [d for d in out if d.status.value == status]
        out.sort(key=lambda d: d.created_at, reverse=True)
        return out[:limit]

    def docket_chain(self, cluster_id: str) -> list[Docket]:
        rows = _zip_get(self.dockets.get(include=["documents", "metadatas"]), None)
        chain = [_row_to_docket(r) for r in rows if r["metadata"].get("cluster_id") == cluster_id]
        chain.sort(key=lambda d: d.attempt)
        return chain

    def close_docket(self, docket_id: str) -> Docket:
        """The hippocampus's last step: every operation decided and applied
        (or the chain ended terminal), the brief consumed - close the docket
        so no tend examines it again. Only a reviewed docket closes; a pending
        one still owes verdicts."""
        d = self.get_docket(docket_id)
        if d is None:
            raise KeyError(docket_id)
        if d.status == DocketStatus.CLOSED:
            return d
        if d.status != DocketStatus.REVIEWED:
            raise DocketError(f"docket {docket_id} is {d.status.value}; only a reviewed docket closes")
        d.status = DocketStatus.CLOSED
        d.extra["closed_at"] = time.time()
        self._save_docket(d)
        return d

    def _save_docket(self, d: Docket) -> None:
        # replace, not merge: operations is a JSON list that must be rewritten whole
        self.dockets.delete(ids=[d.id])
        self.dockets.add(documents=[d.summary or "(no summary)"],
                         metadatas=[_docket_meta(d)], ids=[d.id])

    # -- review ---------------------------------------------------------------
    def review_docket(self, docket_id: str, decisions: list[dict[str, Any]],
                      note: Optional[str] = None) -> dict[str, Any]:
        """Apply a set of per-operation verdicts.

        decisions: [{"op": <index>, "verdict": "approve"|"deny",
                     "critique": str | None, "edited_content": str | None}]
        Approved operations are applied immediately (long-term changes,
        source shorts archived). Denied ones record the critique for the
        hippocampus to redraft. An operation already decided is refused
        (idempotency over re-doing). edited_content is honoured only on a
        terminal docket.
        """
        d = self.get_docket(docket_id)
        if d is None:
            raise KeyError(docket_id)
        if d.status == DocketStatus.REVIEWED:
            raise DocketError(f"docket '{docket_id}' is already reviewed")
        applied: list[dict[str, Any]] = []
        for dec in decisions:
            try:
                idx = int(dec["op"])
                op = d.operations[idx]
            except (KeyError, ValueError, TypeError, IndexError):
                raise DocketError(f"no operation {dec.get('op')!r} on docket '{docket_id}'")
            if op.status != OpStatus.PENDING:
                raise DocketError(f"operation {idx} is already {op.status.value}")
            verdict = str(dec.get("verdict", "")).lower()
            if verdict == "approve":
                edited = dec.get("edited_content")
                if edited is not None:
                    if not d.terminal:
                        raise DocketError("edited_content is only allowed on a terminal docket; "
                                          "deny with a critique and let the hippocampus redraft")
                    if not str(edited).strip():
                        raise DocketError("edited_content must be non-empty; omit it to apply as-is")
                    op.edited_content = str(edited)
                result = self._apply_operation(d, op)
                op.status = OpStatus.APPROVED
                op.long_term_id = result.get("long_term_id")
                op.note = dec.get("note") or note
                applied.append({"op": idx, **result})
            elif verdict == "deny":
                critique = str(dec.get("critique") or "").strip()
                if not critique:
                    raise DocketError(f"operation {idx}: a critique is required to deny")
                op.status = OpStatus.DENIED
                op.critique = critique
                applied.append({"op": idx, "denied": True})
            else:
                raise DocketError(f"operation {idx}: verdict must be approve or deny")
            op.reviewed_at = time.time()
        if all(op.status != OpStatus.PENDING for op in d.operations):
            d.status = DocketStatus.REVIEWED
            d.reviewed_at = time.time()
        self._save_docket(d)
        return {
            "docket_id": d.id, "status": d.status.value,
            "approved": sum(1 for op in d.operations if op.status == OpStatus.APPROVED),
            "denied": sum(1 for op in d.operations if op.status == OpStatus.DENIED),
            "pending": sum(1 for op in d.operations if op.status == OpStatus.PENDING),
            "results": applied,
        }

    # -- apply ----------------------------------------------------------------
    def _long_row(self, entry_id: str) -> Optional[dict[str, Any]]:
        rows = _zip_get(self.long.get(ids=[entry_id], include=["documents", "metadatas"]), None)
        return rows[0] if rows else None

    def _archive_sources(self, ids: list[str]) -> int:
        if not ids:
            return 0
        existing = self.short.get(ids=ids, include=["documents", "metadatas"])
        rows = _zip_get(existing, None)
        if rows:
            self.archive_pruned(rows)
            self.delete_short([r["id"] for r in rows])
        return len(rows)

    def _apply_operation(self, d: Docket, op: DocketOperation) -> dict[str, Any]:
        content = op.edited_content if op.edited_content is not None else op.content
        base_extra = {"from_docket_id": d.id, "docket_op": op.index,
                      "cluster_id": d.cluster_id or d.id,
                      "source_short_ids": list(op.source_short_ids)}
        if op.edited_content is not None:
            base_extra["edited_on_review"] = True
            base_extra["original_op_content"] = op.content

        if op.kind == DocketOpKind.NEW_CORE:
            entry = LongTermEntry(content=content, topic=op.topic, kind="core",
                                  evidence_count=max(1, op.evidence_count),
                                  source=Source.CONSOLIDATOR, extra=base_extra)
            self.add_long(entry)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "shorts_archived": archived}

        if op.kind == DocketOpKind.VERBATIM:
            entry = LongTermEntry(content=content, topic=op.topic, kind="core",
                                  evidence_count=1, source=Source.CONSOLIDATOR,
                                  extra={**base_extra, "preserved_verbatim": True})
            self.add_long(entry)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "shorts_archived": archived}

        if op.kind == DocketOpKind.ATTACH:
            core = self._long_row(op.target_core_id)
            if core is None:
                raise DocketError(f"core '{op.target_core_id}' is gone")
            sat_id = None
            if (content or "").strip():
                sat = LongTermEntry(content=content, topic=op.topic or core["metadata"].get("topic"),
                                    kind="satellite", core_id=op.target_core_id,
                                    evidence_count=max(1, op.evidence_count),
                                    source=Source.CONSOLIDATOR, extra=base_extra)
                self.add_long(sat)
                sat_id = sat.id
            # the core grows: evidence, freshness, and possibly its wording
            meta = dict(core["metadata"])
            meta["evidence_count"] = int(meta.get("evidence_count", 1) or 1) + max(1, op.evidence_count)
            meta["last_confirmed"] = time.time()
            restated = op.restated_content
            if restated and restated.strip() and restated.strip() != core["content"]:
                self._restate_long(op.target_core_id, restated.strip(), meta)
            else:
                self.long.update(ids=[op.target_core_id], metadatas=[_clean_meta(meta)])
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": op.target_core_id, "satellite_id": sat_id,
                    "kind": "attach", "restated": bool(restated),
                    "evidence_count": meta["evidence_count"], "shorts_archived": archived}

        if op.kind == DocketOpKind.SUPERSEDE:
            old = self._long_row(op.target_core_id)
            if old is None:
                raise DocketError(f"core '{op.target_core_id}' is gone")
            entry = LongTermEntry(content=content, topic=op.topic or old["metadata"].get("topic"),
                                  kind="core", evidence_count=max(1, op.evidence_count),
                                  source=Source.CONSOLIDATOR,
                                  extra={**base_extra, "supersedes": op.target_core_id})
            self.add_long(entry)
            self.supersede_long(op.target_core_id, entry.id)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "superseded": op.target_core_id,
                    "shorts_archived": archived}

        raise DocketError(f"unknown operation kind {op.kind!r}")

    def _restate_long(self, entry_id: str, new_content: str, meta: dict[str, Any]) -> None:
        """Replace a core's wording. The distilled retrieval key is re-embedded
        the way a live write embeds it; the old wording is kept in metadata."""
        from .collections import _retrieval_text
        prior = meta.get("restated_from")
        meta["restated_from"] = prior if prior else self._long_row(entry_id)["content"]
        meta["restated_at"] = time.time()
        try:
            vec = self.long._embedding_function([_retrieval_text(new_content, meta.get("topic"))])[0]
            self.long.update(ids=[entry_id], documents=[new_content],
                             embeddings=[[float(x) for x in vec]], metadatas=[_clean_meta(meta)])
        except Exception:  # noqa: BLE001 - let chroma embed the document rather than lose the restate
            self.long.update(ids=[entry_id], documents=[new_content], metadatas=[_clean_meta(meta)])

    # -- the surroundings -------------------------------------------------------
    def satellites_of(self, core_id: str) -> list[dict[str, Any]]:
        rows = _zip_get(self.long.get(include=["documents", "metadatas"]), None)
        sats = [r for r in rows if r["metadata"].get("core_id") == core_id]
        sats.sort(key=lambda r: r["metadata"].get("created_at", 0))
        return sats
