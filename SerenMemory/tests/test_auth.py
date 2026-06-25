"""
Auth middleware tests.

Verifies that bearer token enforcement works correctly:
- public routes (/, /health, /viewer) are always accessible
- all other routes are blocked without a valid token
- a correct token grants access
- a wrong token is rejected
"""
from __future__ import annotations

import pytest

from seren_memory.config import MemoryConfig, ConsolidatorConfig, ServerConfig


TOKEN = "supersecrettoken"


@pytest.fixture
def authed_client(make_client):
    return make_client(
        MemoryConfig(
            server=ServerConfig(bearer_token=TOKEN),
            consolidator=ConsolidatorConfig(enabled=False),
        ),
        raise_server_exceptions=True,
    )


def test_public_routes_no_token(authed_client):
    assert authed_client.get("/").status_code == 200
    assert authed_client.get("/health").status_code == 200
    # /viewer must load without a token so the user can see the UI and
    # enter their token - the API calls the viewer makes are still gated.
    assert authed_client.get("/viewer").status_code in (200, 404)  # 404 if halls.html missing in test env


def test_protected_route_no_token_is_401(authed_client):
    r = authed_client.get("/short")
    assert r.status_code == 401
    assert r.json()["detail"] == "unauthorized"


def test_protected_route_wrong_token_is_401(authed_client):
    r = authed_client.get("/short", headers={"Authorization": "Bearer wrongtoken"})
    assert r.status_code == 401


def test_protected_route_correct_token(authed_client):
    r = authed_client.get("/short", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200


def test_post_protected_with_token(authed_client):
    r = authed_client.post(
        "/short",
        json={"content": "auth test entry", "topic": "auth"},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 200
    assert r.json()["ok"]


def test_search_protected_without_token(authed_client):
    r = authed_client.post("/search", json={"query": "anything"})
    assert r.status_code == 401
