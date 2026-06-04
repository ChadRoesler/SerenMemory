"""
seren_memory.models.schemas
════════════════════════════════════════════════════════════════════════

The data shapes for the three memory tiers. These are the contract - both
the HTTP API and the consolidator agree on these shapes.

THE THREE TIERS (the Halls of Memory):

    ShortTerm  - working memory. ~8 day lifetime. Free read/write. This is
                 the context offloader: stash a thing, retrieve it, drop it.
                 FIFO-ish: oldest ages out unless promoted.

    NearTerm   - open loops. Future-tense intents with trigger conditions.
                 Lives until fulfilled or expired. "Let's do that tomorrow."
                 Free read/write for CREATION; consolidator handles cleanup.

    LongTerm   - consolidated knowledge. Durable. The ONLY gated tier:
                 reads are open, but writes happen exclusively through the
                 consolidator during its periodic window. No surgical edits
                 (the Lacuna Inc. boundary). Supersede, don't delete.

Each entry carries enough metadata for the consolidator to make decisions
without re-deriving everything from the content.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> float:
    """Wall-clock unix timestamp. Used everywhere a 'when' is needed.

    Wall clock, not monotonic, because these timestamps persist across
    process restarts and need to mean the same thing tomorrow. Monotonic
    clocks reset on reboot - useless for 'how old is this memory.'
    """
    return time.time()


def _new_id() -> str:
    """Fresh UUID4 hex string for an entry id."""
    return uuid.uuid4().hex


# ─────────────────────────────────────────────────────────────────────────
#  Provenance - who wrote this, so the consolidator (and Chad) can reason
#  about trust. A memory written by the user is a different kind of fact
#  than one Rhys inferred, which is different from one the consolidator
#  synthesized from many short-term entries.
# ─────────────────────────────────────────────────────────────────────────
class Source(str, Enum):
    USER = "user"               # Chad said this, directly
    ASSISTANT = "assistant"     # Rhys wrote this about the conversation
    CONSOLIDATOR = "consolidator"  # synthesized during consolidation
    AGENT = "agent"             # system/automation wrote it
    BRIEF = "brief"             # came from a daily brief


# ─────────────────────────────────────────────────────────────────────────
#  ShortTerm
# ─────────────────────────────────────────────────────────────────────────
class ShortTermEntry(BaseModel):
    """A working-memory item. Cheap to write, expected to be transient."""

    content: str = Field(..., description="The memory text itself.")
    topic: Optional[str] = Field(
        None, description="Optional topic tag. Consolidator clusters by this + similarity.")
    source: Source = Field(default=Source.ASSISTANT)
    ts: float = Field(default_factory=_now, description="When written (unix).")
    id: str = Field(default_factory=_new_id)

    # Consolidator hint: if Rhys KNOWS this matters, set it. Pinned entries
    # survive consolidation regardless of clustering. The escape hatch for
    # "this only happened once but it's important."
    pinned: bool = Field(default=False)

    # Free-form extra metadata. Chroma stores flat metadata, so this gets
    # flattened on write (see collections.py). Keep values to str/int/float/bool.
    extra: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
#  NearTerm - the open loops
# ─────────────────────────────────────────────────────────────────────────
class TriggerType(str, Enum):
    """How a near-term intent knows it's time to surface."""
    TIME = "time"          # surface after trigger_value (unix ts)
    EVENT = "event"        # surface when a named event matches (e.g. "mentions:balatro")
    ALWAYS = "always"      # surface on every relevant query (a standing note)


class NearTermEntry(BaseModel):
    """A future-tense intent. 'Do X later' / 'bring up Y next time.'"""

    intent: str = Field(..., description="What should happen / be remembered.")
    topic: Optional[str] = Field(None)
    source: Source = Field(default=Source.ASSISTANT)

    trigger_type: TriggerType = Field(default=TriggerType.TIME)
    # For TIME: a unix ts after which this is 'due'.
    # For EVENT: a match string like "mentions:balatro" or "service_down:llama".
    # For ALWAYS: ignored.
    trigger_value: Optional[str] = Field(None)

    created_at: float = Field(default_factory=_now)
    # Auto-drop after this unix ts even if never fulfilled. None = no expiry
    # (the consolidator will still review long-unfulfilled intents and may
    # let them go, but it won't auto-delete on a clock).
    expires_at: Optional[float] = Field(None)

    # Set true when the intent has been ACTED ON (not merely referenced).
    # Consolidator promotes completed intents to LongTerm as a record.
    completed: bool = Field(default=False)
    completed_at: Optional[float] = Field(None)

    id: str = Field(default_factory=_new_id)
    extra: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
#  LongTerm - consolidated, durable, gated
# ─────────────────────────────────────────────────────────────────────────
class LongTermEntry(BaseModel):
    """A consolidated fact. Written ONLY by the consolidator."""

    content: str = Field(..., description="The durable, consolidated statement.")
    topic: Optional[str] = Field(None)

    # How many short-term entries supported this consolidation. Higher =
    # more confident. Used as a recall-ranking multiplier so well-evidenced
    # facts outrank one-offs.
    evidence_count: int = Field(default=1)

    created_at: float = Field(default_factory=_now)
    # Bumped each time the consolidator sees fresh evidence reinforcing this.
    last_confirmed: float = Field(default_factory=_now)

    # Supersession (the non-destructive update path). When a fact changes
    # ("favorite color blue → yellow"), the consolidator writes a NEW entry
    # and sets the old one's superseded_by to the new id. Default recall
    # filters out superseded entries; history queries can still find them.
    superseded_by: Optional[str] = Field(None)

    # forget-flag handling. When user/Rhys flags a memory, the consolidator
    # records WHY here rather than silently deleting. PII gets purged; "I
    # disagree" gets demoted. Never a surgical delete from outside.
    forget_flag: Optional[str] = Field(None, description="Reason a forget was requested.")

    source: Source = Field(default=Source.CONSOLIDATOR)
    id: str = Field(default_factory=_new_id)
    extra: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
#  Consolidator drafts - the model-review gate between synthesis and
#  long-term commit.
#
#  Each cluster synthesis lands here as a DraftEntry (status=pending) for
#  the main model to review. The model can approve (commits to long-term,
#  source shorts archived to pruned) or reject with a critique (triggers
#  a consolidator redraft using the critique as steering). Redrafts form a
#  chain tied by cluster_id; after max_redraft_attempts the chain flips to
#  requires_selection and the model picks the best attempt.
#
#  Verbatim peel-off and direct-promote bypass this gate - both already
#  carry an explicit model review-in-advance signal.
# ─────────────────────────────────────────────────────────────────────────
class DraftStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    # All redraft attempts exhausted: the main model must pick the best
    # of the chain via POST /drafts/{id}/select. Source shorts stay in
    # place until the selection commits.
    REQUIRES_SELECTION = "requires_selection"


class DraftEntry(BaseModel):
    """A consolidator cluster-synthesis awaiting model review before long-term commit."""

    content: str = Field(..., description="The synthesized candidate text.")
    topic: Optional[str] = Field(None)
    evidence_count: int = Field(default=1,
        description="How many short-term entries supported this synthesis.")

    # The cluster's source short-term IDs. On approve, these get archived
    # to pruned (the insurance-window path) and removed from short. On
    # reject, they stay in short for the next consolidation pass.
    source_short_ids: list[str] = Field(default_factory=list)

    # Which brief steered this synthesis, if any. Useful for "why did the
    # consolidator surface THIS cluster" - the answer is usually 'because
    # the brief said so'.
    brief_id_used: Optional[str] = Field(None)

    # Redraft chain tracking. cluster_id ties all attempts for one cluster
    # together (set to the first draft's id; inherited by all redrafts).
    # attempt counts from 1. previous_draft_ids is the ordered chain of
    # earlier attempts so the model (and the redraft prompt) can compare.
    cluster_id: Optional[str] = Field(None,
        description="Stable id shared by all redraft attempts for this cluster.")
    attempt: int = Field(default=1,
        description="Which synthesis attempt this is (1-based).")
    previous_draft_ids: list[str] = Field(default_factory=list,
        description="Ordered ids of earlier attempts in this chain.")

    created_at: float = Field(default_factory=_now)
    status: DraftStatus = Field(default=DraftStatus.PENDING)
    reviewed_at: Optional[float] = Field(None)

    # On reject: the model's critique that steered the redraft.
    # On approve: optional confirmation note.
    critique: Optional[str] = Field(None,
        description="Model critique on reject; optional note on approve.")

    # On approve or select, the resulting long-term entry's id gets recorded
    # here. Forward link so we can answer 'what did this draft become' from
    # the draft side.
    long_term_id: Optional[str] = Field(None)

    # ── Edit-on-select audit trail ──
    # When all redraft attempts are rejected and the chain flips to
    # requires_selection, the editor (the same model role that did the
    # reviews) can commit the best attempt AS-IS or with revisions. If
    # revisions are made, the edited text lives here while the original
    # synthesis stays in `content` - keeps the audit trail intact so we
    # can always see what the consolidator wrote vs what the editor
    # committed. The long-term entry uses edited_content if set, else
    # content. Only meaningful on select (not on approve, which is the
    # "this is fine as-is" path - if you'd want to edit, reject with a
    # critique and let the loop do its job).
    edited_content: Optional[str] = Field(None,
        description="On select: the editor's revised content. The long-term "
                    "entry uses this if set; content stays as the original "
                    "synthesis for audit.")

    source: Source = Field(default=Source.CONSOLIDATOR)
    id: str = Field(default_factory=_new_id)
    extra: dict[str, Any] = Field(default_factory=dict)


# ─────────────────────────────────────────────────────────────────────────
#  Daily brief - the main model's "here's what mattered" note that steers
#  the consolidator's search. Input to consolidation; also a LongTerm
#  candidate itself.
# ─────────────────────────────────────────────────────────────────────────
class DailyBrief(BaseModel):
    """Main model's end-of-cycle summary. Steers consolidation."""

    # Free-text summary of the period. The consolidator reads this for
    # 'what to look for' in short-term.
    summary: str = Field(...)

    # Intents the main model marked as completed this cycle. The
    # consolidator promotes these from NearTerm to LongTerm.
    completed_intents: list[str] = Field(default_factory=list)

    # Topics/phrases the main model flags as worth promoting. The
    # consolidator weights short-term entries matching these higher.
    promote_hints: list[str] = Field(default_factory=list)

    # Topics the main model flags as noise (don't promote even if frequent).
    noise_hints: list[str] = Field(default_factory=list)

    created_at: float = Field(default_factory=_now)
    id: str = Field(default_factory=_new_id)


# ─────────────────────────────────────────────────────────────────────────
#  Unified search - query across all three tiers, ranked.
# ─────────────────────────────────────────────────────────────────────────
class SearchRequest(BaseModel):
    query: str
    n_results: int = Field(default=5, ge=1, le=50)
    # Which tiers to search. Default all three.
    include_short: bool = True
    include_near: bool = True
    include_long: bool = True
    # Filter out superseded long-term entries (the usual case). Set false
    # for "what did Chad USED to think" history queries.
    include_superseded: bool = False


class SearchHit(BaseModel):
    tier: str                      # "short" | "near" | "long"
    content: str
    topic: Optional[str]
    score: float                   # final ranked score (higher = more relevant)
    raw_distance: float            # cosine distance from chroma (lower = closer)
    id: str
    metadata: dict[str, Any]


class SearchResponse(BaseModel):
    query: str
    hits: list[SearchHit]
    searched_tiers: list[str]

# ─────────────────────────────────────────────────────────────────────────
#  Consolidator runs - operational record of each dream-cycle.
#
#  Each call to Consolidator.run_once() emits one of these on completion
#  (success, error, OR noop - the try/finally guarantees it). Persisting
#  these gives the cluster a durable answer to "when did I last consolidate"
#  and a substrate for the Halls viewer's operational dashboard panel
#  (promotion rates over time, run duration trend, etc.).
#
#  The fields mirror the report dict run_once() already builds - we're not
#  adding new instrumentation, just persisting what's already being computed.
# ─────────────────────────────────────────────────────────────────────────
class ConsolidatorRunStatus(str, Enum):
    """Outcome of one consolidation pass."""
    SUCCESS = "success"   # ran and did work
    NOOP = "noop"         # ran cleanly but nothing needed doing
    ERROR = "error"       # raised an exception (recorded anyway)


class ConsolidatorRun(BaseModel):
    """One consolidator pass, recorded for observability."""

    started_at: float
    finished_at: float
    duration_seconds: float
    status: ConsolidatorRunStatus

    # Counters - same as report dict in run_once()
    promoted: int = 0
    drafted: int = 0
    aged_out: int = 0
    near_expired: int = 0
    near_completed_promoted: int = 0
    forget_flags_handled: int = 0
    pruned_swept: int = 0

    # Which brief steered this run, and whether the assistant left it (push)
    # or the consolidator had to fabricate one from short-term (pull).
    brief_id_used: Optional[str] = None
    brief_was_pulled: bool = False

    # If status == ERROR, the exception message (type + str)
    error: Optional[str] = None

    # Counts AFTER the run (mirrors report["counts_after"]; JSON-encoded
    # into chroma metadata by _clean_meta).
    counts_after: dict[str, Any] = Field(default_factory=dict)

    created_at: float = Field(default_factory=_now)
    id: str = Field(default_factory=_new_id)