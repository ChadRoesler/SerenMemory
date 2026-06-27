"""
Unified search - /search.

Queries all three tiers in parallel and merges results by weighted rank.
This is THE recall path - the thing the main model calls to pull relevant
memory into context. One call, all tiers, ranked.

THE RANKING MODEL (this is where the memory hierarchy becomes behavior):

    raw similarity -> chroma gives cosine distance (lower = closer)
    we convert to a base score (1 / (1 + distance)) so higher = better

    then apply a tier weight:
        short × 1.0   - working memory, what's immediately relevant
        near  × 0.9   - active intents, slightly below working memory
        long  × 0.8   - durable facts, individually lower BUT...

    ...long-term gets an evidence multiplier:
        × (1 + log(evidence_count) × 0.15)
    so a long-term fact seen 10 times beats a one-off short-term match.
    A fact you've confirmed over and over SHOULD outrank a passing mention.

The weights are tunable (config could expose them later). The shape -
recency-biased but confidence-corrected - is the point.
"""
from __future__ import annotations

import math

from fastapi import APIRouter, Body, Request

from ..models.schemas import (
    SearchRequest, SearchHit, SearchResponse,
    TopicSearchRequest, TopicHit, TopicSearchResponse,
)

router = APIRouter(tags=["search"])

# Tier base weights. See module docstring for rationale.
_TIER_WEIGHT = {"short": 1.0, "near": 0.9, "long": 0.8}


@router.post("/search")
async def search(request: Request, req: SearchRequest = Body(...)) -> SearchResponse:
    store = request.app.state.store
    searched: list[str] = []
    all_hits: list[SearchHit] = []

    # Over-fetch from each tier (n_results * 2) so the merge has enough
    # candidates to rank meaningfully, then trim to n_results at the end.
    fetch_n = req.n_results * 2

    tiers = []
    if req.include_short:
        tiers.append("short")
    if req.include_near:
        tiers.append("near")
    if req.include_long:
        tiers.append("long")

    for tier in tiers:
        searched.append(tier)
        try:
            raw = store.query(tier, req.query, fetch_n)
        except Exception:  # noqa: BLE001
            continue

        for hit in raw:
            meta = hit["metadata"]

            # Long-term filtering: skip superseded unless asked.
            if tier == "long" and not req.include_superseded:
                if meta.get("superseded_by"):
                    continue

            # Near-term filtering: skip completed (they're history, awaiting
            # promotion - not active loops).
            if tier == "near" and meta.get("completed"):
                continue

            distance = hit["distance"]
            base = 1.0 / (1.0 + max(distance, 0.0))
            score = base * _TIER_WEIGHT[tier]

            # Long-term evidence boost.
            if tier == "long":
                ev = meta.get("evidence_count", 1)
                if isinstance(ev, (int, float)) and ev > 0:
                    score *= (1.0 + math.log(ev) * 0.15)

            all_hits.append(SearchHit(
                tier=tier,
                content=hit["content"],
                topic=meta.get("topic"),
                score=round(score, 6),
                raw_distance=round(distance, 6),
                id=hit["id"],
                metadata=meta,
            ))

    # Merge + rank + trim.
    all_hits.sort(key=lambda h: h.score, reverse=True)
    return SearchResponse(
        query=req.query,
        hits=all_hits[:req.n_results],
        searched_tiers=searched,
    )


@router.post("/by_topic")
async def by_topic(request: Request,
                   req: TopicSearchRequest = Body(...)) -> TopicSearchResponse:
    """Association recall - entries TAGGED with any of `topics`, by EXACT tag
    match, NOT vector similarity (see MemoryStore.query_by_topic). The read-side
    of the topic tags the consolidator clusters on: it surfaces an entry that
    shares a topic with the query even when its wording put it far away in
    vector space - the association edge /search misses (the scar phrased in
    failure-language). Ranked by association STRENGTH (how many requested tags
    an entry carries) then recency; each hit carries matched_topics + overlap
    so the caller sees WHY it surfaced. exclude_ids omits hits the caller
    already has, so an edge join after /search returns only NEW context."""
    store = request.app.state.store
    searched = [t for t, inc in (("short", req.include_short),
                                 ("near", req.include_near),
                                 ("long", req.include_long)) if inc]
    rows = store.query_by_topic(
        req.topics, req.n_results,
        include_short=req.include_short, include_near=req.include_near,
        include_long=req.include_long, include_superseded=req.include_superseded,
        exclude_ids=req.exclude_ids,
    )
    hits = [TopicHit(
        tier=r["tier"], content=r["content"], topic=r["metadata"].get("topic"),
        matched_topics=r["matched_topics"], overlap=r["overlap"],
        id=r["id"], metadata=r["metadata"],
    ) for r in rows]
    return TopicSearchResponse(topics=req.topics, hits=hits, searched_tiers=searched)
