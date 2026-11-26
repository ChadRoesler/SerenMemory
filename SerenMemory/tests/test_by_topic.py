"""
Topic-recall (/by_topic) tests - the ASSOCIATION edge.

Unlike /search (vector similarity), /by_topic retrieves by EXACT tag match over
a cheap .get() (pure SQLite, no embedder), ranked by association strength
(shared-tag count) then recency. Because it never touches the HNSW index, it's
immune to the "segment not flushed" flakiness the vector path guards against -
so these can seed and immediately read back in the same session, including the
long tier (seeded directly via the store, since long-term writes are
consolidator-gated through the HTTP API).

Mirrors the 9 algorithm cases proven for MemoryStore.query_by_topic, at the
HTTP layer against a real chroma store (FakeEmbedder, tmp_path).
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig
from seren_memory.models.schemas import LongTermEntry


@pytest.fixture
def client(make_client):
    # Consolidator off: these tests exercise recall, not the dream cycle.
    return make_client(MemoryConfig(
        consolidator=ConsolidatorConfig(enabled=False),
    ))


def _add_short(client, content, topic):
    r = client.post("/short", json={"content": content, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _add_near(client, intent, topic):
    r = client.post("/near", json={"intent": intent, "topic": topic})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _by_topic(client, topics, **kw):
    body = {"topics": topics, **kw}
    r = client.post("/by_topic", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(resp):
    return [h["id"] for h in resp["hits"]]


# -- core match ---------------------------------------------------------------
def test_single_tag_matches_across_tiers(client):
    s = _add_short(client, "deploy failed, IAM role ARN never passed", "cft, iam")
    n = _add_near(client, "follow up on CFT lambda", "cft, lambda")
    resp = _by_topic(client, ["cft"])
    ids = _ids(resp)
    assert s in ids and n in ids
    for h in resp["hits"]:
        assert "cft" in h["matched_topics"]
        assert h["overlap"] >= 1


def test_no_topic_and_nonmatching_skipped(client):
    _add_short(client, "unrelated lunch note", "food")
    # entry with no topic at all
    r = client.post("/short", json={"content": "no topic entry"})
    assert r.status_code == 200, r.text
    resp = _by_topic(client, ["cft"])
    assert resp["hits"] == []


# -- ranking ------------------------------------------------------------------
def test_multi_tag_overlap_ranks_first(client):
    strong = _add_short(client, "follow up on CFT lambda wiring", "cft, lambda")
    weak = _add_short(client, "how to write a CFT", "cft, howto")
    resp = _by_topic(client, ["cft", "lambda"])
    ids = _ids(resp)
    # Strong (2 shared tags) ranks ahead of weak (1).
    assert ids.index(strong) < ids.index(weak)
    top = resp["hits"][0]
    assert top["id"] == strong and top["overlap"] == 2
    assert sorted(top["matched_topics"]) == ["cft", "lambda"]


# -- exclude ------------------------------------------------------------------
def test_exclude_ids_drops_seen(client):
    s1 = _add_short(client, "cft thing one", "cft")
    s2 = _add_short(client, "cft thing two", "cft")
    resp = _by_topic(client, ["cft"], exclude_ids=[s1])
    ids = _ids(resp)
    assert s1 not in ids and s2 in ids


# -- tier filters -------------------------------------------------------------
def test_completed_near_skipped(client):
    n = _add_near(client, "cft loop", "cft")
    assert client.post(f"/near/{n}/complete").status_code == 200
    resp = _by_topic(client, ["cft"], include_short=False, include_long=False)
    assert n not in _ids(resp)


def test_tier_flags_restrict_scan(client):
    s = _add_short(client, "cft short", "cft")
    _add_near(client, "cft near", "cft")
    resp = _by_topic(client, ["cft"], include_near=False, include_long=False)
    assert resp["searched_tiers"] == ["short"]
    assert all(h["tier"] == "short" for h in resp["hits"])
    assert s in _ids(resp)


def test_superseded_skipped_unless_requested(client):
    # Long-term is consolidator-gated over HTTP; seed via the store directly.
    store = client.app.state.store
    live = LongTermEntry(content="durable cft fact", topic="cft, durable")
    old = LongTermEntry(content="old cft fact", topic="cft")
    store.add_long(live)
    store.add_long(old)
    store.supersede_long(old.id, live.id)

    default = _by_topic(client, ["cft"], include_short=False, include_near=False)
    assert old.id not in _ids(default) and live.id in _ids(default)

    withsup = _by_topic(client, ["cft"], include_short=False, include_near=False,
                        include_superseded=True)
    assert old.id in _ids(withsup)


# -- normalization ------------------------------------------------------------
def test_case_insensitive(client):
    s = _add_short(client, "cft thing", "cft")
    resp = _by_topic(client, ["CFT"])
    assert s in _ids(resp)


def test_empty_topics_returns_empty(client):
    _add_short(client, "cft thing", "cft")
    assert _by_topic(client, [])["hits"] == []
    assert _by_topic(client, ["  ", ""])["hits"] == []


# -- shape --------------------------------------------------------------------
def test_hit_fields_present(client):
    _add_short(client, "cft thing", "cft, iam")
    resp = _by_topic(client, ["cft", "iam"], n_results=1)
    assert resp["hits"], "expected a hit"
    h = resp["hits"][0]
    for field in ("tier", "content", "topic", "matched_topics", "overlap", "id", "metadata"):
        assert field in h, f"missing field: {field}"
    assert h["overlap"] == 2 and sorted(h["matched_topics"]) == ["cft", "iam"]


# -- with a question (26 Nov 2026): the tags decide who is in, the question orders them --------
def test_with_a_question_the_nearest_tagged_entry_leads_whatever_its_tier(client):
    # A match is usually on ONE tag, so everything tied and the date decided alone:
    # short-term, always the newest, won every time. Memory is three stores with one
    # embedder, so inside it a distance is a fair ruler.
    store = client.app.state.store
    core = LongTermEntry(content="the licence moved from GPL to AGPL so nobody can resell the gift", topic="license, history")
    store.add_long(core)
    noise = _add_short(client, "restarted the build box after lunch", "license")
    plain = _by_topic(client, ["license"])
    assert _ids(plain)[:2] == [noise, core.id], "without a question: newest first, as it always was"
    assert all(h["raw_distance"] is None for h in plain["hits"])
    asked = _by_topic(client, ["license"], query="why did the licence move from GPL to AGPL")
    assert _ids(asked)[:2] == [core.id, noise], "with one: the settled core that is about it leads this week's stray note"
    d = {h["id"]: h["raw_distance"] for h in asked["hits"]}
    assert d[core.id] is not None and d[core.id] < d[noise]
    assert {h["id"]: h["matched_topics"] for h in asked["hits"]} == {core.id: ["license"], noise: ["license"]}, \
        "who is in is still the tags' business"


def test_a_tie_goes_to_short_then_long(client):
    store = client.app.state.store
    core = LongTermEntry(content="the same words in both places", topic="tie")
    store.add_long(core)
    short = _add_short(client, "the same words in both places", "tie")
    assert _ids(_by_topic(client, ["tie"], query="the same words")) == [short, core.id]
    short2 = _add_short(client, "another pair with the same words", "tie2")
    core2 = LongTermEntry(content="another pair with the same words", topic="tie2")
    store.add_long(core2)
    assert _ids(_by_topic(client, ["tie2"])) == [core2.id, short2], "no question: the newer one, here the core"
    assert _ids(_by_topic(client, ["tie2"], query="another pair")) == [short2, core2.id], "a question and a tie: short before long"


def test_more_shared_tags_still_lead_a_nearer_entry(client):
    two = _add_short(client, "nothing to do with the question at all", "alpha, beta")
    near = _add_short(client, "why the licence moved", "alpha")
    assert _ids(_by_topic(client, ["alpha", "beta"], query="why the licence moved")) == [two, near]
