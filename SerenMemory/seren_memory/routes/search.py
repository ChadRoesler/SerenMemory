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

A CORE IS AS NEAR AS ITS NEAREST SATELLITE (2 Oct 2026). Recall returns
cores, and satellites were simply dropped from the rows the vector search
returned. But a satellite is an episode - dated, concrete, worded the way a
question is worded - so it is often the closest row in the store, and the
core it hangs under came back nowhere: the long tier fetched 2n rows, found
them full of one subject's own episodes, and threw them all away. (Seen on
the hippocampus's side the same day: a pile about a well-recorded subject
was shown none of its cores.) Now a satellite hit counts for its core: the
core takes the satellite's distance when that is the better one, and the
hit carries `matched_via` - the episode that found it.

The weights are tunable (config could expose them later). The shape -
recency-biased but confidence-corrected - is the point.
"""
from __future__ import annotations

import logging
import math

from fastapi import APIRouter, Body, Request

from ..models.schemas import (
    SearchRequest, SearchHit, SearchResponse,
    TopicSearchRequest, TopicHit, TopicSearchResponse,
)

router = APIRouter(tags=["search"])
log = logging.getLogger("seren_memory.search")

# Tier base weights. See module docstring for rationale.
_TIER_WEIGHT = {"short": 1.0, "near": 0.9, "long": 0.8}


@router.post("/search")
async def search(request: Request, req: SearchRequest = Body(...)) -> SearchResponse:
    store = request.app.state.store
    searched: list[str] = []
    all_hits: list[SearchHit] = []
    via: dict[str, dict] = {}                  # core id -> the nearest satellite of it that matched

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
        except Exception as e:  # noqa: BLE001
            # A tier that cannot answer is left out of the merge, but never
            # silently: an empty long tier reads to the hippocampus as 'no
            # existing cores' and it proposes a duplicate (mem-search-flake).
            log.warning("search: the %s tier failed and was left out: %s: %s", tier, type(e).__name__, e)
            continue

        for hit in raw:
            meta = hit["metadata"]

            # Long-term filtering: satellites are the surroundings, not the
            # answer, and superseded cores are history - both off unless asked.
            if tier == "long" and not req.include_satellites and meta.get("kind") == "satellite":
                cid = str(meta.get("core_id") or "")
                if cid:
                    prev = via.get(cid)
                    if prev is None or hit["distance"] < prev["raw_distance"]:
                        via[cid] = {"id": hit["id"], "content": hit["content"],
                                    "created_at": meta.get("created_at"), "raw_distance": round(hit["distance"], 6)}
                continue
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

    # A core is as near as its nearest satellite (module docstring).
    if via:
        _lift_cores_by_satellite(store, all_hits, via, include_superseded=req.include_superseded)

    # Merge + rank + trim.
    all_hits.sort(key=lambda h: h.score, reverse=True)
    top = all_hits[:req.n_results]
    _attach_surroundings(store, top, inline=req.with_surroundings)
    return SearchResponse(
        query=req.query,
        hits=top,
        searched_tiers=searched,
    )


def _long_score(distance: float, meta: dict) -> float:
    base = 1.0 / (1.0 + max(distance, 0.0))
    score = base * _TIER_WEIGHT["long"]
    ev = meta.get("evidence_count", 1)
    if isinstance(ev, (int, float)) and ev > 0:
        score *= (1.0 + math.log(ev) * 0.15)
    return round(score, 6)


def _lift_cores_by_satellite(store, all_hits: list[SearchHit], via: dict[str, dict], *,
                             include_superseded: bool) -> None:
    """For every satellite the vector search returned: its core takes the
    satellite's distance when that is the better one, and says which episode
    found it. A core the search did not return at all is fetched and added;
    a superseded core stays history unless asked for."""
    present = {h.id: h for h in all_hits if h.tier == "long"}
    for cid, sat in via.items():
        h = present.get(cid)
        if h is not None:
            if sat["raw_distance"] < h.raw_distance:
                h.raw_distance = sat["raw_distance"]
                h.score = _long_score(sat["raw_distance"], h.metadata)
                h.matched_via = sat
            continue
        row = store.get_by_id(cid)
        if row is None or row.get("tier") != "long":
            continue
        meta = row.get("metadata") or {}
        if meta.get("kind", "core") != "core":
            continue
        if meta.get("superseded_by") and not include_superseded:
            continue
        all_hits.append(SearchHit(
            tier="long", content=row.get("content") or "", topic=meta.get("topic"),
            score=_long_score(sat["raw_distance"], meta), raw_distance=sat["raw_distance"],
            id=cid, metadata=meta, matched_via=sat))


def _attach_surroundings(store, hits: list[SearchHit], *, inline: bool, recent: int = 3) -> None:
    """Give every long-tier CORE hit its surroundings: how many satellites
    stand behind it and when the latest landed, what it superseded, what
    superseded it. With inline=True the most recent satellites and the
    superseded core ride along in full. Having the surroundings is one thing;
    a hit that does not say they exist is how they stay unread."""
    cores = [h for h in hits if h.tier == "long" and h.metadata.get("kind", "core") != "satellite"]
    if not cores:
        return
    rows = store.long.get(include=["documents", "metadatas"])
    ids = rows.get("ids") or []
    docs = rows.get("documents") or []
    metas = rows.get("metadatas") or []
    by_id = {i: (docs[k] if k < len(docs) else "", metas[k] or {}) for k, i in enumerate(ids)}
    sats: dict[str, list[tuple[float, str, str]]] = {}
    for i, (doc, meta) in by_id.items():
        cid = meta.get("core_id")
        if cid:
            sats.setdefault(str(cid), []).append((float(meta.get("created_at", 0) or 0), i, doc))
    for h in cores:
        mine = sorted(sats.get(h.id, []), reverse=True)
        sup = h.metadata.get("supersedes")
        out: dict = {
            "satellites": len(mine),
            "latest_satellite_at": mine[0][0] if mine else None,
            "supersedes": sup,
            "superseded_by": h.metadata.get("superseded_by"),
        }
        if inline:
            out["recent"] = [{"id": i, "content": doc, "created_at": ts} for ts, i, doc in mine[:recent]]
            if h.matched_via:
                # the episode that found this core rides first: it is the reason the hit is here
                rest = [r for r in out["recent"] if r["id"] != h.matched_via.get("id")]
                out["recent"] = [{"id": h.matched_via["id"], "content": h.matched_via.get("content"),
                                  "created_at": h.matched_via.get("created_at")}] + rest[: max(0, recent - 1)]
            if sup and sup in by_id:
                out["supersedes_entry"] = {"id": sup, "content": by_id[sup][0],
                                           "created_at": by_id[sup][1].get("created_at")}
        h.surroundings = out


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
        include_satellites=req.include_satellites,
        exclude_ids=req.exclude_ids,
    )
    hits = [TopicHit(
        tier=r["tier"], content=r["content"], topic=r["metadata"].get("topic"),
        matched_topics=r["matched_topics"], overlap=r["overlap"],
        id=r["id"], metadata=r["metadata"],
    ) for r in rows]
    return TopicSearchResponse(topics=req.topics, hits=hits, searched_tiers=searched)
