"""
Unified search - /search.

Queries all three tiers in parallel and merges results by weighted rank.
This is THE recall path - the thing the main model calls to pull relevant
memory into context. One call, all tiers, ranked.

THE RANKING MODEL lives in seren_memory.ranking (29 Oct 2026): a hit's score
is its cosine similarity plus small additive nudges - working memory wins a
tie, a confirmed core wins a near tie, and a real difference in relevance is
never overruled by a tier. The multipliers that were here before (1/(1+d) x
tier weight x evidence) let short-term entries crowd intents and cores out of
the top 50 for a month; the module's docstring has the numbers.

A CORE IS AS NEAR AS ITS NEAREST SATELLITE (2 Oct 2026). Recall returns
cores, and satellites were simply dropped from the rows the vector search
returned. But a satellite is an episode - dated, concrete, worded the way a
question is worded - so it is often the closest row in the store, and the
core it hangs under came back nowhere: the long tier fetched 2n rows, found
them full of one subject's own episodes, and threw them all away. (Seen on
the hippocampus's side the same day: a pile about a well-recorded subject
was shown none of its cores.) Now a satellite hit counts for its core: the
core takes the satellite's distance when that is the better one, and the
hit carries `matched_via` - the episode that found it. History does not lift
(the pull, below): a demoted core and the satellites that were about the old
state hang under the new core for the trail, and never carry it into a
question about the past.

The shape - relevance first, nudged towards working memory and towards what
has been confirmed - is the point; the numbers are ranking.py's.
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Body, Request

from .. import ranking
from ..collections import satellite_parents
from ..models.schemas import (
    SearchRequest, SearchHit, SearchResponse,
    TopicSearchRequest, TopicHit, TopicSearchResponse,
)

router = APIRouter(tags=["search"])
log = logging.getLogger("seren_memory.search")

# THE PULL (29 Oct 2026). When a core is superseded, its satellites move under
# the core that replaced it (draft._demote_into), each tested against the new
# core: one that still supports it keeps its pull (carry) and may lift it; one
# that was about the old state, and the demoted core itself, hang on as
# history with a broken pull and never lift. Measured on 20 Oct 2026 before
# it was built: history that lifted dragged the current core into every
# question about the past (Vellmarch episode 0.82 to 0.46); history that
# hangs without lifting cost nothing. HISTORY_LIFTS is the harness's A/B
# switch for the old behaviour, not a setting.
HISTORY_LIFTS = False


def _broken_pull(meta: dict) -> bool:
    """History: a demoted core (was_core AND superseded_by: a retell's kept
    wording is was_core too, but it is the same story and keeps its pull), or
    a satellite that moved under a superseding core without carrying."""
    if HISTORY_LIFTS or meta.get("carry"):
        return False
    if meta.get("was_core") and meta.get("superseded_by"):
        return True
    return bool(meta.get("moved_from"))


@router.post("/search")
async def search(request: Request, req: SearchRequest = Body(...)) -> SearchResponse:
    store = request.app.state.store
    searched: list[str] = []
    all_hits: list[SearchHit] = []
    via: dict[str, dict] = {}                  # core id -> the nearest satellite of it that matched
    # core id -> every satellite of it the vector search returned, by id, with
    # its distance. Kept only when the caller wants a core's surroundings in
    # MATCH order: the rows are already here (the search fetched them and used
    # to keep one per core), so ordering by them costs no second query and no
    # second embedding of the question.
    by_match = req.with_surroundings and req.surroundings_by == "match"
    near: dict[str, dict[str, dict]] = {}

    # Over-fetch from each tier (n_results * 2) so the merge has enough
    # candidates to rank meaningfully, then trim to n_results at the end.
    fetch_n = req.n_results * 2

    # STRENGTH (ranking.STRENGTH, off unless set): what stands behind each
    # core, counted once per search - its satellites, and the shorts in orbit
    # around it now. One scan of long and short metadata; nothing when the
    # terms are zero.
    strength_extra = _strength_counts(store) if ranking.strength_on() else {}

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
            if by_match and tier == "long" and meta.get("kind") == "satellite":
                # History included: it never LIFTS a core (below), but once the
                # core is in the packet its trail is surroundings like any
                # other, as it already is in "recent" order.
                for cid in satellite_parents(meta):
                    near.setdefault(cid, {})[hit["id"]] = {
                        "id": hit["id"], "content": hit["content"], "created_at": meta.get("created_at"),
                        "raw_distance": round(hit["distance"], 6)}
            if tier == "long" and not req.include_satellites and meta.get("kind") == "satellite":
                if _broken_pull(meta):                      # history hangs on the core and never lifts it
                    continue
                for cid in satellite_parents(meta):          # a shared episode finds each of its cores
                    prev = via.get(cid)
                    if prev is None or hit["distance"] < prev["raw_distance"]:
                        via[cid] = {"id": hit["id"], "content": hit["content"], "tier": "long",
                                    "created_at": meta.get("created_at"), "raw_distance": round(hit["distance"], 6)}
                continue
            if tier == "long" and not req.include_superseded:
                if meta.get("superseded_by"):
                    continue

            # Near-term filtering: skip completed (they're history, awaiting
            # promotion - not active loops).
            if tier == "near" and meta.get("completed"):
                continue

            # LOW CORE ORBIT (config.OrbitConfig): a short that matched brings
            # the cores it orbits up with it, as a satellite does - the core
            # takes the short's distance when that is the better one, and the
            # short rides as matched_via. The durable context arrives with the
            # fresh one in one pass. Resolved at read time: a core gone or
            # superseded since the orbit was marked is skipped by the lift.
            # The short itself is still its own hit, below.
            if tier == "short":
                for cid, _ in store.orbit_of(meta):
                    prev = via.get(cid)
                    if prev is None or hit["distance"] < prev["raw_distance"]:
                        via[cid] = {"id": hit["id"], "content": hit["content"], "tier": "short",
                                    "created_at": meta.get("ts"), "raw_distance": round(hit["distance"], 6)}

            distance = hit["distance"]
            all_hits.append(SearchHit(
                tier=tier,
                content=hit["content"],
                topic=meta.get("topic"),
                score=ranking.score(tier, distance, meta, strength_extra.get(hit["id"])),
                raw_distance=round(distance, 6),
                id=hit["id"],
                metadata=meta,
            ))

    # A core is as near as its nearest satellite (module docstring).
    if via:
        _lift_cores_by_satellite(store, all_hits, via, include_superseded=req.include_superseded,
                                 strength_extra=strength_extra)

    # Merge + rank + trim.
    all_hits.sort(key=lambda h: h.score, reverse=True)
    top = all_hits[:req.n_results]
    # The recall ledger (seren_memory.hits): a satellite that was the episode
    # behind a core in the FINAL packet is counted once. A core found again
    # and again through the same episode is how a promotion gets noticed.
    try:
        store.hits.bump({h.matched_via["id"] for h in top
                         if h.matched_via and h.matched_via.get("tier") == "long" and h.matched_via.get("id")})
    except Exception as e:  # noqa: BLE001 - a count never fails a search
        log.warning("search: could not count satellite hits (%s: %s)", type(e).__name__, e)
    _attach_surroundings(store, top, inline=req.with_surroundings, recent=req.surroundings_n,
                         near=near if by_match else None)
    return SearchResponse(
        query=req.query,
        hits=top,
        searched_tiers=searched,
    )


def _long_score(distance: float, meta: dict, extra: Optional[dict] = None) -> float:
    return ranking.score("long", distance, meta, extra)


def _strength_counts(store) -> dict[str, dict]:
    """Per core: how many satellites hang under it and how many shorts orbit
    it now, for ranking.strength. One read of each tier's metadata."""
    import time as _time
    now = _time.time()
    out: dict[str, dict] = {}
    try:
        rows = store.long.get(include=["metadatas"])
        for meta in rows.get("metadatas") or []:
            for cid in satellite_parents(meta or {}):
                out.setdefault(cid, {"satellites": 0, "orbiting": 0, "now": now})["satellites"] += 1
        for r in store.get_short_all():
            for cid, _ in store.orbit_of(r.get("metadata") or {}):
                out.setdefault(cid, {"satellites": 0, "orbiting": 0, "now": now})["orbiting"] += 1
    except Exception as e:  # noqa: BLE001 - strength is a nudge; a search never fails on it
        log.warning("search: could not count strength (%s: %s); ranking without it", type(e).__name__, e)
    return out


def _lift_cores_by_satellite(store, all_hits: list[SearchHit], via: dict[str, dict], *,
                             include_superseded: bool, strength_extra: Optional[dict] = None) -> None:
    """For every satellite the vector search returned: its core takes the
    satellite's distance when that is the better one, and says which episode
    found it. A core the search did not return at all is fetched and added;
    a superseded core stays history unless asked for."""
    strength_extra = strength_extra or {}
    present = {h.id: h for h in all_hits if h.tier == "long"}
    for cid, sat in via.items():
        h = present.get(cid)
        if h is not None:
            if sat["raw_distance"] < h.raw_distance:
                h.raw_distance = sat["raw_distance"]
                h.score = _long_score(sat["raw_distance"], h.metadata, strength_extra.get(cid))
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
            score=_long_score(sat["raw_distance"], meta, strength_extra.get(cid)), raw_distance=sat["raw_distance"],
            id=cid, metadata=meta, matched_via=sat))


def _by_match(mine: list[tuple[float, str, str]], matched: dict[str, dict], via: Optional[dict],
              n: int) -> list[dict]:
    """A core's satellites in MATCH order, at most n: the episode it was found
    through, the others that matched nearest first, then the newest. Every row
    says its raw_distance (None where the episode did not match). `mine` is
    every satellite of the core, newest first."""
    out: list[dict] = []
    seen: set[str] = set()

    def add(row: dict) -> None:
        if row.get("id") and row["id"] not in seen and len(out) < n:
            seen.add(row["id"])
            out.append(row)

    if via and via.get("id"):
        add({"id": via["id"], "content": via.get("content"), "created_at": via.get("created_at"),
             "raw_distance": via.get("raw_distance")})
    when = {i: ts for ts, i, _ in mine}           # one created_at per episode, whichever way it is listed
    for m in sorted(matched.values(), key=lambda m: (m["raw_distance"], m["id"])):
        add({**m, "created_at": when.get(m["id"], m.get("created_at"))})
    for ts, i, doc in mine:
        add({"id": i, "content": doc, "created_at": ts, "raw_distance": None})
    return out


def _attach_surroundings(store, hits: list[SearchHit], *, inline: bool, recent: int = 3,
                         near: Optional[dict[str, dict[str, dict]]] = None) -> None:
    """Give every long-tier CORE hit its surroundings: how many satellites
    stand behind it and when the latest landed, what it superseded, what
    superseded it. With inline=True the most recent satellites and the
    superseded core ride along in full. Having the surroundings is one thing;
    a hit that does not say they exist is how they stay unread.

    `recent` is how many satellites ride per core. `near` (a core's satellites
    that matched the question, by id, with their distances) switches the order
    from newest-first to MATCH (see _by_match). Newest-first handed a question
    about one episode the three latest ones: on the Vellmarch ladder (24 Nov
    2026) detail questions scored the same on a 1,000- and a 4,000-token page,
    because a bigger page never brought a different episode."""
    cores = [h for h in hits if h.tier == "long" and h.metadata.get("kind", "core") != "satellite"]
    if not cores:
        return
    rows = store.long.get(include=["documents", "metadatas"])
    ids = rows.get("ids") or []
    docs = rows.get("documents") or []
    metas = rows.get("metadatas") or []
    by_id = {i: (docs[k] if k < len(docs) else "", metas[k] or {}) for k, i in enumerate(ids)}
    sats: dict[str, list[tuple[float, str, str]]] = {}
    parents: dict[str, list[str]] = {}          # satellite id -> every core it hangs on
    for i, (doc, meta) in by_id.items():
        on = satellite_parents(meta)
        if on:
            parents[i] = on
        for cid in on:
            sats.setdefault(cid, []).append((float(meta.get("created_at", 0) or 0), i, doc))
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
            if recent <= 0:
                out["recent"] = []
            elif near is not None:
                out["recent"] = _by_match(mine, near.get(h.id) or {}, h.matched_via, recent)
            else:
                out["recent"] = [{"id": i, "content": doc, "created_at": ts} for ts, i, doc in mine[:recent]]
                if h.matched_via:
                    # the episode that found this core rides first: it is the reason the hit is here
                    rest = [r for r in out["recent"] if r["id"] != h.matched_via.get("id")]
                    out["recent"] = [{"id": h.matched_via["id"], "content": h.matched_via.get("content"),
                                      "created_at": h.matched_via.get("created_at")}] + rest[: max(0, recent - 1)]
            # An episode that hangs on other cores too says which (Chad, 25 Nov
            # 2026: "sats that hang on two core bring their other core id").
            # Ids, not content: a pointer costs next to nothing to carry, and
            # get_satellites on one of them is the second look for whoever
            # wants it. Absent when this core is the episode's only home.
            for r in out["recent"]:
                others = [c for c in parents.get(r.get("id"), []) if c != h.id]
                if others:
                    r["also_core_ids"] = others
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
