"""
Validation / bad-input tests.

Every endpoint that accepts a body should reject garbage gracefully (422),
and certain semantic rules (e.g. forget requires a reason) should return 400.
This file documents the contract for callers - and catches regressions when
schemas change.
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(
        consolidator=ConsolidatorConfig(enabled=False),
    ))


# -- /short ----------------------------------------------------------------

def test_short_missing_content_is_422(client):
    r = client.post("/short", json={"topic": "no_content_field"})
    assert r.status_code == 422


def test_short_empty_body_is_422(client):
    r = client.post("/short", json={})
    assert r.status_code == 422


def test_short_non_json_body_is_422(client):
    r = client.post("/short", content=b"not json at all", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


# -- /near -----------------------------------------------------------------

def test_near_missing_intent_is_422(client):
    r = client.post("/near", json={"topic": "no_intent"})
    assert r.status_code == 422


def test_near_empty_body_is_422(client):
    r = client.post("/near", json={})
    assert r.status_code == 422


# -- /search ---------------------------------------------------------------

def test_search_missing_query_is_422(client):
    r = client.post("/search", json={"n_results": 5})
    assert r.status_code == 422


def test_search_n_results_zero_is_422(client):
    r = client.post("/search", json={"query": "something", "n_results": 0})
    assert r.status_code == 422


def test_search_n_results_over_limit_is_422(client):
    r = client.post("/search", json={"query": "something", "n_results": 9999})
    assert r.status_code == 422


def test_search_empty_body_is_422(client):
    r = client.post("/search", json={})
    assert r.status_code == 422


# -- /long forget ----------------------------------------------------------

def test_forget_missing_reason_is_400(client):
    r = client.post("/long/nonexistent/forget", json={})
    assert r.status_code == 400


def test_forget_empty_reason_is_400(client):
    r = client.post("/long/nonexistent/forget", json={"reason": ""})
    assert r.status_code == 400


# -- /brief ----------------------------------------------------------------

def test_brief_missing_summary_is_422(client):
    r = client.post("/brief", json={"promote_hints": ["something"]})
    assert r.status_code == 422


def test_brief_empty_body_is_422(client):
    r = client.post("/brief", json={})
    assert r.status_code == 422


# -- method not allowed ----------------------------------------------------

def test_post_long_directly_is_405(client):
    r = client.post("/long", json={"content": "should not work"})
    assert r.status_code == 405
