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
