"""Functional tests for the mounted MCP HTTP endpoint.

The existing test_mcp_mount.py checks the SHAPE of the mount (is there a
/mcp route, is it an ASGI app). That shape was green while THREE separate
bugs made the endpoint unreachable in practice:

  1. double-/mcp: streamable_http_app() serves at settings.streamable_http_path
     (default "/mcp"), and mounting THAT at "/mcp" pushed the real endpoint to
     "/mcp/mcp" - so "/mcp" itself 404'd.
  2. dead task group: a mounted sub-app's lifespan doesn't fire under
     Starlette, so session_manager.run() was never entered and every request
     500'd with "Task group is not initialized".
  3. host check: FastMCP's DNS-rebinding protection defaults to localhost-only,
     421'ing the cross-host (memory-host:7420) access the route exists for.

These tests drive an actual JSON-RPC `initialize` through the live app (with
the lifespan entered, the way uvicorn runs it) so the whole path is exercised.
A shape test can't catch a 404/500/421; only a request can.

Gated on the `mcp` SDK like the rest of the MCP suite.
"""
from __future__ import annotations

import json

import pytest

try:
    import mcp  # noqa: F401
    _mcp_available = True
except ImportError:
    _mcp_available = False

pytestmark = pytest.mark.skipif(
    not _mcp_available, reason="mcp extras not installed"
)

from seren_memory.config import ConsolidatorConfig, MemoryConfig

# StreamableHTTP requires BOTH content types advertised or it 406s.
_MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "seren-test", "version": "0"},
    },
}


@pytest.fixture
def mcp_client(make_client):
    """Live app with the MCP route mounted and its lifespan entered
    (make_client calls TestClient.__enter__, which runs startup → the
    session manager task group is live)."""
    return make_client(
        MemoryConfig(consolidator=ConsolidatorConfig(enabled=False)),
        raise_server_exceptions=False,
    )


def _body_text(resp) -> str:
    # streamable-HTTP frames the reply as an SSE event ("event: message\n
    # data: {...}"); just return raw text and let callers substring/parse.
    return resp.text


def test_initialize_handshake_succeeds_at_mcp(mcp_client):
    """POST initialize to /mcp returns 200 with a JSON-RPC result. This one
    assertion would have failed on ALL THREE bugs (404, 500, or 421)."""
    r = mcp_client.post("/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:300]}"
    text = _body_text(r)
    assert "result" in text and "protocolVersion" in text, \
        f"no initialize result in body: {text[:300]}"


def test_mcp_trailing_slash_also_works(mcp_client):
    """`http://memory-host:7420/mcp/` (with the slash - the form some clients send)
    must resolve too, not 404."""
    r = mcp_client.post("/mcp/", json=_INIT, headers=_MCP_HEADERS,
                        follow_redirects=True)
    assert r.status_code == 200, f"trailing-slash form 404'd again: {r.status_code}"


def test_double_mcp_path_is_gone(mcp_client):
    """Regression guard for bug 1 specifically: the OLD broken endpoint
    location must NOT answer. If "/mcp/mcp" ever starts working again, the
    sub-app's internal path drifted back to its "/mcp" default and the real
    endpoint moved out from under "/mcp"."""
    r = mcp_client.post("/mcp/mcp", json=_INIT, headers=_MCP_HEADERS)
    assert r.status_code == 404, \
        f"/mcp/mcp answered ({r.status_code}) - the double-mount is back"


def test_missing_accept_header_is_not_404(mcp_client):
    """Sanity boundary: a request to the RIGHT path with the WRONG headers
    should fail with a transport error (406/400), NOT 404. This is the
    diagnostic that distinguishes 'path wrong' (404) from 'path right,
    request malformed' - the exact signal that localizes future breakage."""
    r = mcp_client.post("/mcp", json=_INIT,
                        headers={"Content-Type": "application/json"})
    assert r.status_code != 404, \
        "right path should not 404 on a header problem; 404 means the route moved"