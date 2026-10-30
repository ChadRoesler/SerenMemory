"""
seren_memory.ranking (29 Oct 2026, mem-update-rank): relevance first, nudged.

- a nearer entry outranks a farther one whatever their tiers: a long core at
  distance 0.4 beats a short entry at 0.6, which the old multipliers flipped
- at equal relevance working memory wins, and a confirmed core wins a near tie
- an evidence bonus is a nudge, never a contest: ten confirmations cannot
  lift a weak match over a good one
- the shipped shape is still there behind mode "inverse", and reproduces its
  old numbers, so the harness can put the two side by side
- /search and the MCP recall score with the same formula
"""
from __future__ import annotations

import math

import pytest

from seren_memory import ranking
from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture(autouse=True)
def shipped_shape():
    """Every test starts from the module's defaults and leaves them."""
    saved = ranking.settings()
    ranking.configure(mode="similarity", tier_bonus={"short": 0.02, "near": 0.05, "long": 0.0},
                      evidence_bonus=0.015, tier_weight={"short": 1.0, "near": 0.9, "long": 0.8},
                      evidence_weight=0.15)
    yield
    ranking.configure(mode=saved["mode"], tier_bonus=saved["tier_bonus"], evidence_bonus=saved["evidence_bonus"],
                      tier_weight=saved["tier_weight"], evidence_weight=saved["evidence_weight"])


def test_a_nearer_entry_outranks_a_farther_one_whatever_the_tiers():
    core = ranking.score("long", 0.4, {"evidence_count": 1})
    short = ranking.score("short", 0.6, {})
    intent = ranking.score("near", 0.52, {})
    assert core > intent > short, (core, intent, short)
    # the old shape flipped exactly this: 1/(1.4)*0.8 = 0.571 lost to 1/(1.6)*1.0 = 0.625
    ranking.configure(mode="inverse")
    assert ranking.score("long", 0.4, {"evidence_count": 1}) < ranking.score("short", 0.6, {})


def test_at_equal_relevance_a_promise_then_working_memory_win_and_a_confirmed_core_wins_a_near_tie():
    assert ranking.score("near", 0.5, {}) > ranking.score("short", 0.5, {}) > ranking.score("long", 0.5, {})
    assert ranking.score("near", 0.5, {}) - ranking.score("long", 0.5, {}) == pytest.approx(0.05, abs=1e-6)
    once, ten = ranking.score("long", 0.5, {"evidence_count": 1}), ranking.score("long", 0.5, {"evidence_count": 10})
    assert ten > once and ten - once == pytest.approx(math.log(10) * 0.015, abs=1e-6)
    assert ranking.score("long", 0.5, {"evidence_count": 10}) > ranking.score("short", 0.5, {}), \
        "ten confirmations outweigh the working-memory nudge at equal relevance"
    assert ranking.score("near", 0.5, {}) > ranking.score("long", 0.5, {"evidence_count": 10}), \
        "but not the promise's: a nudge, not a contest, on both sides"


def test_evidence_is_a_nudge_never_a_contest():
    weak_but_confirmed = ranking.score("long", 0.7, {"evidence_count": 50})
    good = ranking.score("long", 0.5, {"evidence_count": 1})
    assert good > weak_but_confirmed
    assert ranking.evidence({"evidence_count": 0}) == 1.0 and ranking.evidence({"evidence_count": "x"}) == 1.0
    assert ranking.evidence({"evidence_count": True}) == 1.0 and ranking.evidence(None) == 1.0


def test_the_shipped_shape_is_kept_behind_inverse_and_reproduces_its_numbers():
    ranking.configure(mode="inverse")
    assert ranking.score("short", 0.25, {}) == pytest.approx(0.8, abs=1e-6)
    assert ranking.score("near", 0.25, {}) == pytest.approx(0.72, abs=1e-6)
    assert ranking.score("long", 0.25, {"evidence_count": 10}) == pytest.approx(0.8 * 0.8 * (1 + math.log(10) * 0.15), abs=1e-6)
    with pytest.raises(ValueError):
        ranking.configure(mode="sideways")
    assert ranking.configure(tier_weight={"long": 1.0}, evidence_weight=0.0)["tier_weight"]["long"] == 1.0
    assert ranking.score("long", 0.25, {"evidence_count": 10}) == pytest.approx(0.8, abs=1e-6)


def test_similarity_is_bounded_and_a_bad_distance_does_not_blow_up():
    assert ranking.score("short", 0.0, {}) == pytest.approx(1.02, abs=1e-6)
    assert ranking.score("long", 2.0, {}) == pytest.approx(-1.0, abs=1e-6)
    assert ranking.score("long", 5.0, {}) == ranking.score("long", 2.0, {})
    assert ranking.score("long", -0.3, {}) == ranking.score("long", 0.0, {})


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(pruned_safety_days=0)))


def test_the_search_route_and_recall_score_with_the_module(client):
    client.post("/short", json={"content": "the night the user promised to bring a lamp if it ever got dark", "topic": "us"})
    client.post("/near", json={"intent": "ask the user for the lamp story again on a quiet night", "topic": "us"})
    body = client.post("/search", json={"query": "lamp promise", "n_results": 5}).json()
    assert body["hits"], "the seeded entries come back"
    for h in body["hits"]:
        assert h["score"] == ranking.score(h["tier"], h["raw_distance"], h["metadata"])
    scores = [h["score"] for h in body["hits"]]
    assert scores == sorted(scores, reverse=True)
