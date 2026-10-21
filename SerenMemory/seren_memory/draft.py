"""
seren_memory.draft
════════════════════════════════════════════════════════════════════════

The draft: what a consolidation pass proposes, and how the store applies
what the reviewer approves.

THE SHAPE

    A draft is not one synthesis that becomes one long-term entry. A draft
    is a DRAFT: a list of operations on long-term, each reviewed on its
    own by the main model, each carrying its own verdict and critique.

        new_core    a durable statement with the lesson in it - the
                    retelling; the episodes it was made from are kept
                    under it as its first satellites (kept_episode)
        attach      tonight's short-terms are evidence for a core that
                    already exists; the core's evidence grows, its wording
                    may be restated
        supersede   yellow arrives; blue becomes a demoted satellite of
                    yellow's story, still recallable through history
        verbatim    "this deserves its own thing and must not be muddled" -
                    one episode, kept word for word as its own core
        retell      the core's wording becomes the retelling (21 Oct 2026);
                    the old wording hangs under it as its episode, no new
                    evidence, no word-share guard - the reviewer is the guard

    Long-term is a core and its surroundings. Recall returns cores.
    Satellites (kind=satellite, core_id=<core>) are the supporting episodes
    with their dates; a superseded core keeps superseded_by pointing forward.
    A core's own sources land under it as satellites. Before that they went 
    to pruned, which recall never sees, so the only place an episode's detail 
    survived promotion was the core's text - and cores came out as long as the 
    episodes they were made from.

WHO DOES WHAT

    The hippocampus (the small model, the Inside Out workers) writes the
    draft from the short-terms the brief steered it to, POST /drafts.
    The main model reviews it through Memory's MCP tools or POST
    /drafts/{id}/review, approving or denying PER OPERATION with a
    critique. THIS module applies approved operations, so the store stays
    consistent and the worker stays stateless. Denied operations go back
    to the hippocampus's tend loop, which resubmits a new draft in the
    same chain (cluster_id, attempt, previous_draft_ids). The last
    permitted attempt is submitted with terminal=true, and only then may
    an approval carry edits (edited_content, edited_kind,
    edited_target_core_id, edited_restated_content) - the editor's release
    valve, never the loop's shortcut. At the last attempt the reviewer
    takes the best of the bunch, edits as needed and approves (the map of
    the cycle): a denial there drops the operation.

WHAT AN APPROVAL CONSUMES
    An approved operation archives the short-terms it names in
    source_short_ids, and only those. The hippocampus names a fragment only
    when the operation's text carries every sentence of it (covered_fragments,
    after a two-subject fragment lost its second subject on 3 Oct 2026); the
    ones it cited but left out ride along as left_short_ids. A small model
    writes short, so a long fragment is often left out of every attempt. It
    stays in short-term beside its long-term copy, and each sleep drafts it
    again. So an approval on ANY attempt may carry edited_source_short_ids: 
    the reviewer saying "this carries those". Unlike the edits above it 
    changes no text, and the loop cannot fix it - a redraft comes back no 
    longer. Nothing tidies short-term against long-term instead: a short-term 
    ranks above long-term in recall, and one shaped like a core can hold the 
    detail the core lost; it leaves by an approval that names it, or by aging out.

A RESTATE CANNOT WIPE A CORE
    An attach may carry restated_content: new wording for the core.
    Approving it REPLACES the core's text. On 1 Oct 2026 the small model put
    the episode's own text there, a woken reviewer approved without
    comparing, and two cores were overwritten with their satellites. So the
    review checks it (restate_problem): text that is the episode itself, or
    that drops most of the core, is refused. The reviewer approves with
    "restate": false to attach the episode and leave the core's wording
    alone, or "restate": true having compared them.

THE WAY BACK IS GATED TOO
    A core that was restated wrongly can go back to its earlier wording, but
    not by a call that rewrites it on the spot: that would be the scalpel
    the no-delete rule refuses. The restore is gated, a call that lives next
    to the no-delete rule. And it is the model's call, not a person's: there
    is no HTTP route for it, only the MCP tool. The gate is there so the
    person cannot reach in, for the same reason delete is not let in: the
    memory is the model's own.
    It works the way forget does. flag_undo_restate records a request with a
    reason; the hippocampus's tick executes it (/tidy with purge - the step
    that executes flags); the wording that was replaced, the reason and the
    time stay on the core. The only text it can write is text a review
    already approved for that core.

FORGET IS NOT HERE. A purge is a separate path (MemoryStore.purge_long):
    a person's flag, executed with a tombstone and a cascade. Nothing in a
    draft deletes.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from .models.schemas import (
    Draft, DraftOperation, DraftOpKind, DraftStatus, LongTermEntry, OpStatus, Source,
)


class DraftError(ValueError):
    """A review that cannot be applied: bad verdict, unknown op, wrong state."""


_WORD = re.compile(r"[a-z0-9']+")
RESTATE_KEEP = 0.5          # the share of the core's words a restatement must still carry
_TARGETED = (DraftOpKind.ATTACH, DraftOpKind.SUPERSEDE, DraftOpKind.RETELL)   # the kinds that name a core


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if len(w) >= 4}


def restate_problem(core: str, restated: str, episode: str = "") -> Optional[str]:
    """Why this restated_content must not replace this core's wording, or None
    when it reads as the core reworded. A restatement is the WHOLE core said
    again with the new detail merged in. Two things it is not: the episode's
    own text (the satellite), and something that drops most of what the core
    says."""
    r, c = (restated or "").strip(), (core or "").strip()
    if not r or r == c:
        return None
    if episode and r == episode.strip():
        return "is the episode's own text, not the core reworded"
    cw = _words(c)
    if cw:
        kept = len(cw & _words(r)) / len(cw)
        if kept < RESTATE_KEEP:
            return f"keeps only {round(kept * 100)}% of the core's words"
    if len(r) < 0.5 * len(c):
        return "is less than half the length of the core it replaces"
    return None


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
# One chroma document per draft. The document text is the draft's summary
# (what the embedder sees, what a viewer shows); the operations ride in
# metadata as JSON, which is the only place chroma lets a list live.

def _draft_meta(d: Draft) -> dict[str, Any]:
    return _clean_meta({
        "status": d.status.value,
        "cluster_id": d.cluster_id or d.id,
        "attempt": d.attempt,
        "terminal": d.terminal,
        "previous_draft_ids": d.previous_draft_ids,
        "brief_id_used": d.brief_id_used,
        "created_at": d.created_at,
        "reviewed_at": d.reviewed_at,
        "source": d.source.value,
        "operations": [op.model_dump() for op in d.operations],
        **d.extra,
    })


def _row_to_draft(row: dict[str, Any]) -> Draft:
    meta = dict(row["metadata"])
    ops_raw = meta.pop("operations", []) or []
    if isinstance(ops_raw, str):
        ops_raw = _maybe_json(ops_raw) or []
    prev = meta.pop("previous_draft_ids", None) or meta.pop("previous_docket_ids", []) or []
    if isinstance(prev, str):
        prev = _maybe_json(prev) or []
    known = {"status", "cluster_id", "attempt", "terminal", "brief_id_used",
             "created_at", "reviewed_at", "source"}
    extra = {k: v for k, v in meta.items() if k not in known}
    return Draft(
        id=row["id"],
        summary=row["content"],
        status=DraftStatus(meta.get("status", "pending")),
        cluster_id=meta.get("cluster_id") or row["id"],
        attempt=int(meta.get("attempt", 1) or 1),
        terminal=bool(meta.get("terminal", False)),
        previous_draft_ids=list(prev),
        brief_id_used=meta.get("brief_id_used"),
        created_at=float(meta.get("created_at", 0) or 0),
        reviewed_at=meta.get("reviewed_at"),
        source=Source(meta.get("source", Source.CONSOLIDATOR.value)),
        operations=[DraftOperation(**op) for op in ops_raw],
        extra=extra,
    )


class DraftMixin:
    """Mixed into MemoryStore. Needs self.drafts, self.long, self.short,
    self.pruned, self.add_long, self.archive_pruned, self.delete_short,
    self.supersede_long, self.get_by_id."""

    # -- submit / read ---------------------------------------------------------
    def submit_draft(self, d: Draft) -> Draft:
        if not d.operations:
            raise DraftError("a draft needs at least one operation")
        for i, op in enumerate(d.operations):
            op.index = i
        for i, op in enumerate(d.operations):
            if op.target_op is not None:
                self._check_target_op(d, i, op)
            elif op.kind in _TARGETED and not op.target_core_id:
                raise DraftError(f"operation {i} ({op.kind.value}) needs target_core_id"
                                 + (" (or target_op)" if op.kind != DraftOpKind.RETELL else ""))
            if op.kind in _TARGETED and op.target_op is None:
                target = self._long_row(op.target_core_id)
                if target is None:
                    raise DraftError(f"operation {i}: no long-term entry '{op.target_core_id}'")
                if target["metadata"].get("kind", "core") != "core":
                    raise DraftError(f"operation {i}: '{op.target_core_id}' is a satellite, not a core")
            if not (op.content or "").strip() and op.kind != DraftOpKind.ATTACH:
                raise DraftError(f"operation {i} ({op.kind.value}) needs content")
        d.cluster_id = d.cluster_id or d.id
        self.drafts.add(documents=[d.summary or "(no summary)"],
                         metadatas=[_draft_meta(d)], ids=[d.id])
        return d

    @staticmethod
    def _check_target_op(d: Draft, i: int, op: DraftOperation) -> None:
        """target_op: attach to (or supersede) the core ANOTHER operation in
        this draft creates. A dream told as one new core with its details as
        satellites could not be drafted before - the satellites had no core
        id to name until the core was approved, so they attached to the
        nearest wrong one (seen live 28 Sept 2026, hip-draft-deps)."""
        t = op.target_op
        if op.kind not in (DraftOpKind.ATTACH, DraftOpKind.SUPERSEDE):
            raise DraftError(f"operation {i}: target_op is only for attach or supersede, not {op.kind.value}")
        if op.target_core_id:
            raise DraftError(f"operation {i}: give target_core_id or target_op, not both")
        if not 0 <= t < len(d.operations):
            raise DraftError(f"operation {i}: target_op {t} is not an operation in this draft")
        if t == i:
            raise DraftError(f"operation {i}: target_op points at itself")
        if d.operations[t].kind != DraftOpKind.NEW_CORE:
            raise DraftError(f"operation {i}: target_op {t} is a {d.operations[t].kind.value}, not a new_core")

    def get_draft(self, draft_id: str) -> Optional[Draft]:
        rows = _zip_get(self.drafts.get(ids=[draft_id], include=["documents", "metadatas"]), None)
        return _row_to_draft(rows[0]) if rows else None

    def list_drafts(self, status: Optional[str] = None, limit: int = 20) -> list[Draft]:
        rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        out = [_row_to_draft(r) for r in rows]
        if status:
            out = [d for d in out if d.status.value == status]
        out.sort(key=lambda d: d.created_at, reverse=True)
        return out[:limit]

    def draft_chain(self, cluster_id: str) -> list[Draft]:
        rows = _zip_get(self.drafts.get(include=["documents", "metadatas"]), None)
        chain = [_row_to_draft(r) for r in rows if r["metadata"].get("cluster_id") == cluster_id]
        chain.sort(key=lambda d: d.attempt)
        return chain

    def shorts_under_review(self) -> dict[str, str]:
        """short-term id -> the draft that holds it. A short-term is HELD while
        an operation citing it is under review: pending in a pending draft, or
        denied in a reviewed draft whose redraft has not arrived yet (not
        terminal, no later attempt in the chain).

        WHY: review is the gate on long-term. The consolidator promotes what
        is approved, and the gate exists so the model is the one who approves
        its own memories. A memory that is
        mid-review is promoted by that review; promote_memory_now on it went
        around the gate, and left the draft citing something already landed
        (found by a woken reviewer the same day)."""
        held: dict[str, str] = {}
        drafts = self.list_drafts(limit=10 ** 6)
        for d in drafts:
            if d.status == DraftStatus.PENDING:
                for op in d.operations:
                    if op.status == OpStatus.PENDING:
                        for sid in op.source_short_ids:
                            held.setdefault(str(sid), d.id)
            elif d.status == DraftStatus.REVIEWED and not d.terminal:
                denied = [op for op in d.operations if op.status == OpStatus.DENIED]
                if not denied:
                    continue
                cluster = d.cluster_id or d.id
                if any((x.cluster_id or x.id) == cluster and x.attempt > d.attempt for x in drafts):
                    continue                               # the redraft is in; it holds what it cites
                for op in denied:
                    for sid in op.source_short_ids:
                        held.setdefault(str(sid), d.id)
        return held

    def close_draft(self, draft_id: str) -> Draft:
        """The hippocampus's last step: every operation decided and applied
        (or the chain ended terminal), the brief consumed - close the draft
        so no tend examines it again. Only a reviewed draft closes; a pending
        one still owes verdicts."""
        d = self.get_draft(draft_id)
        if d is None:
            raise KeyError(draft_id)
        if d.status == DraftStatus.CLOSED:
            return d
        if d.status != DraftStatus.REVIEWED:
            raise DraftError(f"draft {draft_id} is {d.status.value}; only a reviewed draft closes")
        d.status = DraftStatus.CLOSED
        d.extra["closed_at"] = time.time()
        self._save_draft(d)
        return d

    def _save_draft(self, d: Draft) -> None:
        # replace, not merge: operations is a JSON list that must be rewritten whole
        self.drafts.delete(ids=[d.id])
        self.drafts.add(documents=[d.summary or "(no summary)"],
                         metadatas=[_draft_meta(d)], ids=[d.id])

    # -- review ---------------------------------------------------------------
    def review_draft(self, draft_id: str, decisions: list[dict[str, Any]],
                      note: Optional[str] = None) -> dict[str, Any]:
        """Apply a set of per-operation verdicts.

        decisions: [{"op": <index>, "verdict": "approve"|"deny",
                     "critique": str | None, "edited_content": str | None}]
        Approved operations are applied immediately (long-term changes,
        source shorts archived). Denied ones record the critique for the
        hippocampus to redraft. An operation already decided is refused
        (idempotency over re-doing).

        On a terminal draft only, an approval may carry the reviewer's own
        fixes: edited_content (the text), edited_kind, edited_target_core_id
        (the wrong kind, or aimed at the wrong core), edited_restated_content.

        On any draft, an approved attach may carry "restate": false - attach
        the episode, leave the core's wording alone - or "restate": true -
        the reviewer compared the rewording with the core and wants it. With
        neither, a rewording that would wipe the core is refused (see
        restate_problem).

        On any draft, an approval may carry edited_source_short_ids: the
        short-terms it carries, replacing the drafted list (see WHAT AN
        APPROVAL CONSUMES).
        """
        d = self.get_draft(draft_id)
        if d is None:
            raise KeyError(draft_id)
        if d.status == DraftStatus.REVIEWED:
            raise DraftError(f"draft '{draft_id}' is already reviewed")
        # Every decision is checked before any is applied. Checking as it went,
        # a bad third decision raised after the first two were already in
        # long-term, with the draft still showing them pending - approve them
        # again and they applied twice.
        plan: list[tuple[int, Any, str, dict[str, Any]]] = []
        seen: set[int] = set()
        for dec in decisions:
            try:
                idx = int(dec["op"])
                op = d.operations[idx]
            except (KeyError, ValueError, TypeError, IndexError):
                raise DraftError(f"no operation {dec.get('op')!r} on draft '{draft_id}'")
            if op.status != OpStatus.PENDING:
                raise DraftError(f"operation {idx} is already {op.status.value}")
            if idx in seen:
                raise DraftError(f"operation {idx} is decided twice in one review")
            seen.add(idx)
            verdict = str(dec.get("verdict", "")).lower()
            if verdict == "approve":
                edited = dec.get("edited_content")
                if edited is not None:
                    if not d.terminal:
                        raise DraftError("edited_content is only allowed on a terminal draft; "
                                          "deny with a critique and let the hippocampus redraft")
                    if not str(edited).strip():
                        raise DraftError("edited_content must be non-empty; omit it to apply as-is")
                dec = {**dec, "_final": self._final_shape(d, idx, op, dec)}
            elif verdict == "deny":
                if not str(dec.get("critique") or "").strip():
                    raise DraftError(f"operation {idx}: a critique is required to deny")
            else:
                raise DraftError(f"operation {idx}: verdict must be approve or deny")
            plan.append((idx, op, verdict, dec))

        # An op on a core this draft creates lands only with, or after, that core.
        approving = {idx for idx, _, verdict, _ in plan if verdict == "approve"}
        for idx, op, verdict, _ in plan:
            if verdict != "approve" or op.target_op is None:
                continue
            t = d.operations[op.target_op]
            if (t.status == OpStatus.APPROVED and t.long_term_id) or op.target_op in approving:
                continue
            verb = "attaches to" if op.kind == DraftOpKind.ATTACH else "supersedes"
            if t.status == OpStatus.DENIED:
                raise DraftError(f"operation {idx} {verb} operation {t.index}'s new core, and operation "
                                 f"{t.index} was denied, so that core does not exist; deny operation {idx}")
            raise DraftError(f"operation {idx} {verb} operation {t.index}'s new core; approve operation "
                             f"{t.index} too, or deny operation {idx}")
        # the cores first, so their ids exist when the ops on them apply
        plan.sort(key=lambda p: p[1].target_op is not None)

        applied: list[dict[str, Any]] = []
        try:
            for idx, op, verdict, dec in plan:
                if verdict == "approve":
                    if dec.get("edited_content") is not None:
                        op.edited_content = str(dec["edited_content"])
                    self._take_final_shape(op, dec["_final"])
                    if op.target_op is not None:
                        op.target_core_id = d.operations[op.target_op].long_term_id
                    result = self._apply_operation(d, op)
                    op.status = OpStatus.APPROVED
                    op.long_term_id = result.get("long_term_id")
                    op.note = dec.get("note") or note
                    applied.append({"op": idx, **result})
                else:
                    op.status = OpStatus.DENIED
                    op.critique = str(dec.get("critique")).strip()
                    applied.append({"op": idx, "denied": True})
                op.reviewed_at = time.time()
        except Exception:
            # an apply that fails part way (a target core purged meanwhile)
            # still records what DID land, so nothing is applied twice
            self._save_draft(d)
            raise
        if all(op.status != OpStatus.PENDING for op in d.operations):
            d.status = DraftStatus.REVIEWED
            d.reviewed_at = time.time()
        self._save_draft(d)
        return {
            "draft_id": d.id, "status": d.status.value,
            "approved": sum(1 for op in d.operations if op.status == OpStatus.APPROVED),
            "denied": sum(1 for op in d.operations if op.status == OpStatus.DENIED),
            "pending": sum(1 for op in d.operations if op.status == OpStatus.PENDING),
            "results": applied,
        }

    # -- the shape an approval lands in ----------------------------------------
    def _final_shape(self, d: Draft, idx: int, op: DraftOperation, dec: dict[str, Any]) -> dict[str, Any]:
        """What an approved operation will be once the reviewer's fixes are
        in: its kind, target and restatement. Checked here, before anything
        is applied, so a refused decision changes nothing."""
        ek, et, er = dec.get("edited_kind"), dec.get("edited_target_core_id"), dec.get("edited_restated_content")
        if (ek is not None or et is not None or er is not None) and not d.terminal:
            raise DraftError(f"operation {idx}: edited_kind, edited_target_core_id and edited_restated_content are "
                             "only allowed on a terminal draft; deny with a critique and let the hippocampus redraft")
        kind = op.kind
        if ek is not None:
            try:
                kind = DraftOpKind(str(ek).strip().lower())
            except ValueError:
                raise DraftError(f"operation {idx}: edited_kind must be one of "
                                 f"{', '.join(k.value for k in DraftOpKind)}")
        target, target_op = op.target_core_id, op.target_op
        if kind in (DraftOpKind.NEW_CORE, DraftOpKind.VERBATIM):
            target, target_op = None, None
        elif et is not None:
            target, target_op = str(et).strip(), None
        core_text: Optional[str] = None
        if kind in _TARGETED:
            if target_op is not None:
                t = d.operations[target_op]
                core_text = t.edited_content if t.edited_content is not None else t.content
            else:
                if not target:
                    raise DraftError(f"operation {idx}: a {kind.value} needs a core; give edited_target_core_id")
                row = self._long_row(target)
                if row is None:
                    raise DraftError(f"operation {idx}: no long-term entry '{target}'")
                if row["metadata"].get("kind", "core") != "core":
                    raise DraftError(f"operation {idx}: '{target}' is a satellite, not a core")
                core_text = row["content"]
        content = dec.get("edited_content") if dec.get("edited_content") is not None else op.content
        if kind != DraftOpKind.ATTACH and not str(content or "").strip():
            raise DraftError(f"operation {idx}: a {kind.value} needs content; give edited_content")
        if kind == DraftOpKind.RETELL and str(content or "").strip() == (core_text or "").strip():
            raise DraftError(f"operation {idx}: a retell that keeps the core's wording changes nothing; "
                             "give edited_content, or deny it")
        restated = (str(er) if er is not None else op.restated_content) if kind == DraftOpKind.ATTACH else None
        restated = (restated or "").strip() or None
        choice = dec.get("restate")
        if choice is False:
            restated = None
        if kind == DraftOpKind.ATTACH and not restated and not str(content or "").strip():
            raise DraftError(f"operation {idx}: an attach with no episode text and no rewording does nothing; "
                             "give edited_content, or deny it")
        if restated and choice is not True:
            why = restate_problem(core_text or "", restated, str(content or ""))
            if why:
                raise DraftError(
                    f"operation {idx}: approving this would REPLACE the wording of core {target or 'in this draft'} "
                    f"with text that {why}. Approve with \"restate\": false to attach the episode and keep the "
                    f"core's wording, or with \"restate\": true if you have compared the two and want the "
                    f"rewording, or deny it.")
        return {"kind": kind, "target_core_id": target, "target_op": target_op, "restated_content": restated,
                "source_short_ids": self._edited_sources(idx, op, dec.get("edited_source_short_ids"))}

    def _edited_sources(self, idx: int, op: DraftOperation, edited: Any) -> list[str]:
        """The short-terms an approval consumes: the drafted list, or the
        reviewer's. A name the op did not already carry must be in short-term
        now - a typo would archive nothing and say nothing."""
        if edited is None:
            return list(op.source_short_ids)
        if not isinstance(edited, list) or not all(isinstance(s, str) and s.strip() for s in edited):
            raise DraftError(f"operation {idx}: edited_source_short_ids must be a list of short-term ids")
        ids = list(dict.fromkeys(s.strip() for s in edited))
        new = [s for s in ids if s not in op.source_short_ids]
        present = {r["id"] for r in self._shorts_present(new)}
        gone = [s for s in new if s not in present]
        if gone:
            raise DraftError(f"operation {idx}: not in short-term: {', '.join(gone)} "
                             "(a typo, or it was promoted or aged out)")
        return ids

    @staticmethod
    def _take_final_shape(op: DraftOperation, final: dict[str, Any]) -> None:
        """Put the checked shape on the operation; what was drafted is kept
        in review_edits for the audit."""
        for field in ("kind", "target_core_id", "target_op", "restated_content", "source_short_ids"):
            was, now = getattr(op, field), final[field]
            if was != now:
                op.review_edits[field] = was.value if isinstance(was, DraftOpKind) else was
                setattr(op, field, now)

    # -- what the reviewer needs in front of them ------------------------------
    def review_view(self, draft_id: str) -> Optional[dict[str, Any]]:
        """A draft as its reviewer should see it: each operation beside the
        core it would change, a restatement already checked against that
        core, and - on a redraft - every earlier version of the operation
        with the critique that sent it back. On the last attempt the reviewer
        takes the best of these, edits as needed and approves."""
        d = self.get_draft(draft_id)
        if d is None:
            return None
        chain = {x.attempt: x for x in self.draft_chain(d.cluster_id or d.id)}
        out = d.model_dump()
        for op, view in zip(d.operations, out["operations"]):
            core_text = None
            if op.kind in _TARGETED:
                if op.target_op is not None and 0 <= op.target_op < len(d.operations):
                    core_text = d.operations[op.target_op].content
                    view["target_core"] = {"op": op.target_op, "content": core_text}
                elif op.target_core_id:
                    row = self._long_row(op.target_core_id)
                    core_text = row["content"] if row else None
                    view["target_core"] = {"id": op.target_core_id, "content": core_text,
                                           "gone": row is None}
            if op.kind == DraftOpKind.ATTACH and (op.restated_content or "").strip() and op.status == OpStatus.PENDING:
                why = restate_problem(core_text or "", op.restated_content or "", op.content)
                view["restate_check"] = (
                    f"REFUSED as it stands: the rewording {why}. Approve with \"restate\": false to keep the "
                    f"core's wording." if why else
                    "approving REPLACES target_core's wording with restated_content: compare them first")
            if op.kind == DraftOpKind.RETELL and op.status == OpStatus.PENDING and core_text is not None:
                old_w, new_w = _words(core_text), _words(op.content or "")
                kept = round(100 * len(old_w & new_w) / len(old_w)) if old_w else 100
                view["retell_check"] = (
                    f"approving REPLACES the core's wording ({len(core_text) // 4} tokens) with content "
                    f"({len(op.content or '') // 4} tokens, keeping {kept}% of its words); the old wording hangs "
                    "under the core as its episode, and undo_restate brings it back. Read both: a retelling keeps "
                    "the names, dates, numbers and quotes that matter and adds nothing. Approve with edited_content "
                    "to retell it yourself, or deny it.")
            if op.kind in (DraftOpKind.NEW_CORE, DraftOpKind.SUPERSEDE) and op.status == OpStatus.PENDING:
                same = [r["id"] for r in self._shorts_present(list(op.source_short_ids))
                        if (r.get("content") or "").strip() == (op.content or "").strip()]
                if same:
                    view["core_check"] = (
                        f"content is the episode's own words ({', '.join(same)}). A core is the retelling; the "
                        "episode itself is kept under it as a satellite when this lands. Deny with 'retell it' "
                        "unless these words are the point, or on the last attempt approve with edited_content.")
            earlier, at, ref = [], d.attempt - 1, op.redraft_of
            while ref is not None and at in chain and 0 <= ref < len(chain[at].operations):
                prev = chain[at].operations[ref]
                earlier.append({"attempt": at, "kind": prev.kind.value, "target_core_id": prev.target_core_id,
                                "content": prev.content, "source_short_ids": list(prev.source_short_ids),
                                "critique": prev.critique})
                at, ref = at - 1, prev.redraft_of
            if earlier:
                view["earlier_attempts"] = earlier
            if op.status == OpStatus.PENDING and not (op.kind == DraftOpKind.RETELL and not op.source_short_ids):
                self._sources_check(op, earlier, view)       # a retell of a core cites nothing, by design
        if d.terminal and d.status == DraftStatus.PENDING:
            out["last_attempt"] = (
                "This is the last attempt: an operation denied now is dropped and its memory stays in "
                "short-term. Land each one - approve it, or approve it with your fixes (edited_content, "
                "edited_kind, edited_target_core_id, \"restate\": false, edited_source_short_ids), taking the "
                "best wording from earlier_attempts. Deny only what should not be in memory at all.")
        return out

    def _sources_check(self, op: DraftOperation, earlier: list[dict[str, Any]], view: dict[str, Any]) -> None:
        """What a pending operation leaves in short-term: the fragments the
        hippocampus cited but found its text does not carry, and the ones an
        earlier attempt cited, that are still there. Shown in full, since
        whether the text carries them is the reviewer's call."""
        cited = [*op.left_short_ids, *(s for e in earlier for s in e["source_short_ids"])]
        left = self._shorts_present([s for s in cited if s not in op.source_short_ids])
        if not left and op.source_short_ids:
            return
        if left:
            view["left_in_short_term"] = [{"id": r["id"], "content": r["content"]} for r in left]
        lead = ("names no short-term: approving it archives nothing, so what it was drafted from stays in "
                "short-term and a later sleep drafts it again" if not op.source_short_ids else
                "leaves fragments it cited in short-term")
        view["sources_check"] = (
            f"{lead}. left_in_short_term has the ones still there. If your approval carries one - every "
            "sentence of it, in content or edited_content - approve with edited_source_short_ids listing it "
            "with the rest; if not, leave it for a later sleep." if left else f"{lead}.")

    def _shorts_present(self, ids: list[str]) -> list[dict[str, Any]]:
        """The short-terms among these ids, in the order given."""
        ids = list(dict.fromkeys(ids))
        if not ids:
            return []
        rows = {r["id"]: r for r in _zip_get(self.short.get(ids=ids, include=["documents", "metadatas"]), None)}
        return [rows[s] for s in ids if s in rows]

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

    def _apply_operation(self, d: Draft, op: DraftOperation) -> dict[str, Any]:
        content = op.edited_content if op.edited_content is not None else op.content
        base_extra = {"from_draft_id": d.id, "draft_op": op.index,
                      "cluster_id": d.cluster_id or d.id,
                      "source_short_ids": list(op.source_short_ids)}
        if op.edited_content is not None:
            base_extra["edited_on_review"] = True
            base_extra["original_op_content"] = op.content
        # the pictures the source episodes carry: what is made from them keeps them
        inherited = self.sources_eidetic(list(op.source_short_ids))
        if inherited:
            base_extra["eidetic"] = inherited

        if op.kind == DraftOpKind.NEW_CORE:
            entry = LongTermEntry(content=content, topic=op.topic, kind="core",
                                  evidence_count=max(1, op.evidence_count),
                                  source=Source.CONSOLIDATOR, extra=base_extra)
            self.add_long(entry)
            kept = self._keep_episodes(d, op, entry)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "satellite_ids": kept, "shorts_archived": archived}

        if op.kind == DraftOpKind.VERBATIM:
            entry = LongTermEntry(content=content, topic=op.topic, kind="core",
                                  evidence_count=1, source=Source.CONSOLIDATOR,
                                  extra={**base_extra, "preserved_verbatim": True})
            self.add_long(entry)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "shorts_archived": archived}

        if op.kind == DraftOpKind.ATTACH:
            core = self._long_row(op.target_core_id)
            if core is None:
                raise DraftError(f"core '{op.target_core_id}' is gone")
            sat_id = None
            if (content or "").strip():
                sat = LongTermEntry(content=content, topic=op.topic or core["metadata"].get("topic"),
                                    kind="satellite", core_id=op.target_core_id,
                                    evidence_count=max(1, op.evidence_count),
                                    source=Source.CONSOLIDATOR, extra=base_extra)
                self.add_long(sat)
                sat_id = sat.id
            # the core grows: evidence, freshness, its pictures, and possibly its wording
            meta = dict(core["metadata"])
            meta["evidence_count"] = int(meta.get("evidence_count", 1) or 1) + max(1, op.evidence_count)
            meta["last_confirmed"] = time.time()
            if inherited:
                meta["eidetic"] = self.merge_eidetic(self.eidetic_in(meta), inherited)
            restated = op.restated_content
            if restated and restated.strip() and restated.strip() != core["content"]:
                self._restate_long(op.target_core_id, restated.strip(), meta)
            else:
                self.long.update(ids=[op.target_core_id], metadatas=[_clean_meta(meta)])
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": op.target_core_id, "satellite_id": sat_id,
                    "kind": "attach", "restated": bool(restated),
                    "evidence_count": meta["evidence_count"], "shorts_archived": archived}

        if op.kind == DraftOpKind.SUPERSEDE:
            old = self._long_row(op.target_core_id)
            if old is None:
                raise DraftError(f"core '{op.target_core_id}' is gone")
            # the old core's pictures come forward with the new one; the old keeps its own
            carried = self.merge_eidetic(self.eidetic_in(old["metadata"]), inherited)
            entry = LongTermEntry(content=content, topic=op.topic or old["metadata"].get("topic"),
                                  kind="core", evidence_count=max(1, op.evidence_count),
                                  source=Source.CONSOLIDATOR,
                                  extra={**base_extra, "supersedes": op.target_core_id,
                                         **({"eidetic": carried} if carried else {})})
            self.add_long(entry)
            self.supersede_long(op.target_core_id, entry.id)
            kept = self._keep_episodes(d, op, entry)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": entry.id, "kind": "core", "superseded": op.target_core_id,
                    "satellite_ids": kept, "shorts_archived": archived}

        if op.kind == DraftOpKind.RETELL:
            core = self._long_row(op.target_core_id)
            if core is None:
                raise DraftError(f"core '{op.target_core_id}' is gone")
            old_text = (core["content"] or "").strip()
            meta = dict(core["metadata"])
            sat_id = None
            # the old wording becomes the core's episode - once; a core that was
            # overwritten with its own satellite's text (seen 1 Oct 2026) is not doubled
            already = any((s.get("content") or "").strip() == old_text for s in self.satellites_of(op.target_core_id))
            if old_text and not already:
                own = self.eidetic_in(meta)
                sat = LongTermEntry(content=old_text, topic=meta.get("topic"), kind="satellite",
                                    core_id=op.target_core_id, evidence_count=1, source=Source.CONSOLIDATOR,
                                    created_at=float(meta.get("created_at") or time.time()),
                                    extra={**base_extra, "kept_episode": True, "was_core": True,
                                           **({"eidetic": own} if own else {})})
                self.add_long(sat)
                sat_id = sat.id
            if inherited:
                meta["eidetic"] = self.merge_eidetic(self.eidetic_in(meta), inherited)
            # the wording changes; the evidence does not - a retelling is not new evidence
            self._restate_long(op.target_core_id, (content or "").strip(), meta)
            archived = self._archive_sources(op.source_short_ids)
            return {"long_term_id": op.target_core_id, "kind": "retell", "satellite_id": sat_id,
                    "shorts_archived": archived}

        raise DraftError(f"unknown operation kind {op.kind!r}")

    def _keep_episodes(self, d: Draft, op: DraftOperation, core: LongTermEntry) -> list[str]:
        """The episodes a new core was made from become its first satellites,
        each with its own date, topic and pictures, so the core can be the
        retelling and the detail still has somewhere to live. A source whose
        text IS the core's (verbatim; mechanical mode keeping the longest
        fragment whole) is not doubled under it."""
        ids = list(dict.fromkeys(op.source_short_ids))
        if not ids:
            return []
        rows = _zip_get(self.short.get(ids=ids, include=["documents", "metadatas"]), None)
        rows.sort(key=lambda r: float((r.get("metadata") or {}).get("ts") or 0))
        core_text = (core.content or "").strip()
        kept: list[str] = []
        for r in rows:
            text = (r.get("content") or "").strip()
            if not text or text == core_text:
                continue
            meta = r.get("metadata") or {}
            own = self.eidetic_in(meta)
            sat = LongTermEntry(content=text, topic=core.topic or meta.get("topic"), kind="satellite",
                                core_id=core.id, evidence_count=1, source=Source.CONSOLIDATOR,
                                created_at=float(meta.get("ts") or time.time()),
                                extra={"from_draft_id": d.id, "draft_op": op.index,
                                       "cluster_id": d.cluster_id or d.id, "source_short_ids": [r["id"]],
                                       "kept_episode": True, **({"eidetic": own} if own else {})})
            self.add_long(sat)
            kept.append(sat.id)
        return kept

    def _restate_long(self, entry_id: str, new_content: str, meta: dict[str, Any]) -> None:
        """Replace a core's wording. The distilled retrieval key is re-embedded
        the way a live write embeds it; the old wording is kept in metadata."""
        prior = meta.get("restated_from")
        meta["restated_from"] = prior if prior else self._long_row(entry_id)["content"]
        meta["restated_at"] = time.time()
        self._write_long_text(entry_id, new_content, meta)

    def _earlier_wording(self, entry_id: str) -> tuple[dict[str, Any], str]:
        row = self._long_row(entry_id)
        if row is None:
            raise KeyError(entry_id)
        prior = str(row["metadata"].get("restated_from") or "").strip()
        if not prior or prior == row["content"].strip():
            raise DraftError(f"core '{entry_id}' has no earlier wording to return to")
        return row, prior

    def flag_undo_restate(self, entry_id: str, reason: str) -> dict[str, Any]:
        """Ask for a core's earlier wording back (metadata.restated_from: what
        it said before a restate replaced it - the FIRST wording, if it was
        restated more than once). A request, not the act: the hippocampus's
        tick executes it. A reason is required and is kept."""
        if not (reason or "").strip():
            raise DraftError("a reason is required to ask for a core's earlier wording back")
        row, prior = self._earlier_wording(entry_id)
        meta = dict(row["metadata"])
        meta["undo_restate_flag"] = reason.strip()
        self.long.update(ids=[entry_id], metadatas=[_clean_meta(meta)])
        return {"id": entry_id, "flagged_reason": reason.strip(), "will_read": prior, "reads_now": row["content"]}

    def flagged_restates(self) -> list[dict[str, Any]]:
        return [r for r in self.get_long_all() if r["metadata"].get("undo_restate_flag")]

    def undo_restate(self, entry_id: str) -> dict[str, Any]:
        """Execute a flagged request: the earlier wording goes back, and the
        wording it replaces, the reason and the time stay on the core
        (restate_undone, restate_undone_reason, restate_undone_at). Refused
        without a flag - there is no unflagged way to rewrite a core. The
        replaced text is usually still there as the satellite the same
        approval attached."""
        row, prior = self._earlier_wording(entry_id)
        meta = dict(row["metadata"])
        reason = str(meta.get("undo_restate_flag") or "").strip()
        if not reason:
            raise DraftError(f"core '{entry_id}' is not flagged; flag it with a reason first")
        meta["restate_undone"] = row["content"]
        meta["restate_undone_reason"] = reason
        meta["restate_undone_at"] = time.time()
        meta["undo_restate_flag"] = ""                     # done; chroma merges metadata, so cleared, not dropped
        meta["restated_from"] = prior                      # equal to the content again: nothing left to undo
        self._write_long_text(entry_id, prior, meta)
        return {"id": entry_id, "reason": reason, "restored_at": meta["restate_undone_at"]}

    def _write_long_text(self, entry_id: str, new_content: str, meta: dict[str, Any]) -> None:
        from .collections import _retrieval_text
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
