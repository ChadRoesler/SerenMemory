"""
seren_memory.ranking - the one place a recall hit's score is computed.

THE SHAPE (29 Oct 2026, mem-update-rank). A hit's score is its cosine
SIMILARITY (1 - chroma's cosine distance) plus small ADDITIVE nudges:

    score = similarity + TIER_BONUS[tier] + evidence_bonus

    similarity       what the question asked for: nearly the whole ordering
    TIER_BONUS       near +0.05: at EQUAL relevance a promise comes first
                     (measured 29 Oct: +0.05 lifted Memory alone 0.54 to 0.59
                     at 250 tokens with no class losing; +0.10 was no longer a
                     nudge); short +0.02: then working memory; long +0.00
    evidence_bonus   long only: ln(evidence_count) x EVIDENCE_BONUS (0.015),
                     so a core confirmed ten times gets +0.035: it wins a
                     near tie and never a contest

WHY ADDITIVE ON SIMILARITY, not the old multipliers on 1/(1+d). MiniLM puts a
good match at cosine distance 0.3-0.5 and a weak one at 0.7-0.9. The old base
1/(1+d) squeezed that whole band into 0.77-0.53, and then a 20% tier weight
(long x 0.8) or a 33% evidence multiplier (ten confirmations) outweighed the
entire difference between a right answer and a wrong one. Measured 20 Oct
2026 on a real personal store: three intents ranked 9, 19 and past 50 for
paraphrased questions while short-term entries filled the top four every
time; the core holding a promise the user had made was not in the top 50;
plain cosine over the same records found each one first. Flattening the weights
helped the long tiers at big budgets and hurt short-term at small ones: the
multipliers were trading one tier's precision for another's recall, and no
setting of them won everywhere. Similarity keeps the band (0.5-0.7 against
0.1-0.3), and a nudge of a few hundredths says what it means: ties go to
working memory and to what has been confirmed; a real difference in
relevance is never overruled by a tier.

A core is as near as its nearest satellite (routes.search): the lift hands
the satellite's distance in here and the same formula scores it.

The shipped shape until then ("inverse": 1/(1+d) x tier weight x (1 + ln(ev)
x 0.15)) is kept behind MODE for the side-by-side in Probe's harness
(TestDatasets/baselines/compare_md.py); it is not a setting a store should
run on. The knobs are module attributes on purpose: `configure` is for an
experiment, not a yaml.
"""
from __future__ import annotations

import math
import time
from typing import Any, Optional

MODE = "similarity"                                     # or "inverse": the shape shipped before 29 Oct 2026

# similarity mode
TIER_BONUS: dict[str, float] = {"short": 0.02, "near": 0.05, "long": 0.0}
EVIDENCE_BONUS = 0.015

# STRENGTH (29 Oct 2026: "a stronger, more used, warmer memory roots in
# and means more" - the Boy Scout fire). A core's strength is what the store
# already knows about it beyond its evidence count: how many episodes stand
# behind it (satellites), how many fresh shorts sit in orbit around it now
# (the live warmth, before consolidation), and how recently it was confirmed.
# The trap, measured 20 Oct: strength that beats relevance is exactly what
# buried the current title under eleven satellite-heavy summary cores. So
# the term is additive on similarity like the nudges, and CAPPED: it settles
# ties among near-equal matches and never crosses a real gap. Off (all zero)
# until the harness says what it is worth; the harness sets it in-process.
STRENGTH: dict[str, float] = {"satellites": 0.0, "orbit": 0.0, "warmth": 0.0, "cap": 0.04, "warm_days": 30.0}


def strength_on() -> bool:
    """Whether any strength term is set, so a caller knows to count."""
    return MODE == "similarity" and any(STRENGTH[k] > 0 for k in ("satellites", "orbit", "warmth"))


def strength(meta: Optional[dict[str, Any]], extra: Optional[dict[str, Any]]) -> float:
    """The capped strength of a long-term core: satellites and orbiting shorts
    counted by the caller (extra: satellites, orbiting, now), warmth from the
    core's own last_confirmed."""
    if not strength_on():
        return 0.0
    x = extra or {}
    s = STRENGTH["satellites"] * math.log1p(max(0, int(x.get("satellites", 0) or 0)))
    s += STRENGTH["orbit"] * math.log1p(max(0, int(x.get("orbiting", 0) or 0)))
    if STRENGTH["warmth"] > 0:
        lc = (meta or {}).get("last_confirmed")
        if isinstance(lc, (int, float)) and not isinstance(lc, bool):
            age_days = max(0.0, (float(x.get("now") or time.time()) - float(lc)) / 86400.0)
            s += STRENGTH["warmth"] * max(0.0, 1.0 - age_days / max(STRENGTH["warm_days"], 1e-9))
    return min(s, STRENGTH["cap"])

# inverse mode (the shipped shape, kept for the A/B)
TIER_WEIGHT: dict[str, float] = {"short": 1.0, "near": 0.9, "long": 0.8}
EVIDENCE_WEIGHT = 0.15


def evidence(meta: Optional[dict[str, Any]]) -> float:
    """A long-term entry's evidence_count as a number >= 1."""
    ev = (meta or {}).get("evidence_count", 1)
    return float(ev) if isinstance(ev, (int, float)) and not isinstance(ev, bool) and ev > 0 else 1.0


def score(tier: str, distance: float, meta: Optional[dict[str, Any]] = None,
          extra: Optional[dict[str, Any]] = None) -> float:
    """The score of one hit: `tier` in short / near / long, `distance` the
    cosine distance chroma (or the brute-force fallback) gave, `meta` the
    entry's metadata (evidence_count and last_confirmed are read for the
    long tier), `extra` what the caller counted for a core when strength is
    on (satellites, orbiting, now)."""
    d = max(float(distance), 0.0)
    if MODE == "inverse":
        s = (1.0 / (1.0 + d)) * TIER_WEIGHT.get(tier, 1.0)
        if tier == "long":
            s *= 1.0 + math.log(evidence(meta)) * EVIDENCE_WEIGHT
        return round(s, 6)
    s = 1.0 - min(d, 2.0)                               # cosine distance is 0..2; similarity 1..-1
    s += TIER_BONUS.get(tier, 0.0)
    if tier == "long":
        s += math.log(evidence(meta)) * EVIDENCE_BONUS
        s += strength(meta, extra)
    return round(s, 6)


def settings() -> dict[str, Any]:
    return {"mode": MODE, "tier_bonus": dict(TIER_BONUS), "evidence_bonus": EVIDENCE_BONUS,
            "strength": dict(STRENGTH),
            "tier_weight": dict(TIER_WEIGHT), "evidence_weight": EVIDENCE_WEIGHT}


def configure(*, mode: Optional[str] = None, tier_bonus: Optional[dict[str, float]] = None,
              evidence_bonus: Optional[float] = None, tier_weight: Optional[dict[str, float]] = None,
              evidence_weight: Optional[float] = None,
              strength: Optional[dict[str, float]] = None) -> dict[str, Any]:
    """Change the shape in-process, for an experiment (the harness) or a
    test. Returns the settings now in force."""
    global MODE, EVIDENCE_BONUS, EVIDENCE_WEIGHT
    if mode is not None:
        if mode not in ("similarity", "inverse"):
            raise ValueError(f"ranking mode must be similarity or inverse, not {mode!r}")
        MODE = mode
    if tier_bonus:
        TIER_BONUS.update({k: float(v) for k, v in tier_bonus.items()})
    if evidence_bonus is not None:
        EVIDENCE_BONUS = float(evidence_bonus)
    if tier_weight:
        TIER_WEIGHT.update({k: float(v) for k, v in tier_weight.items()})
    if evidence_weight is not None:
        EVIDENCE_WEIGHT = float(evidence_weight)
    if strength:
        unknown = set(strength) - set(STRENGTH)
        if unknown:
            raise ValueError(f"unknown strength term(s): {', '.join(sorted(unknown))}")
        STRENGTH.update({k: float(v) for k, v in strength.items()})
    return settings()
