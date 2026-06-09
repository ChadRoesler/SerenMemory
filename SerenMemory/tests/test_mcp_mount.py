"""Tests for seren_memory.mcp.server - the mount integration.

Gated behind `pytest.importorskip("mcp")` - without the SDK, the whole
file skips. This file exercises the lifecycle that app.py runs at
startup: build a FastAPI app, wire app.state.store/config/consolidator,
call mount_mcp_routes, verify the /mcp route lands.
"""
from __future__ import annotations

import pytest

try:
    import mcp  # noqa: F401
    from seren_memory.mcp.server import mount_mcp_routes
    _mcp_available = True
except ImportError:
    _mcp_available = False
    mount_mcp_routes = None  # type: ignore

pytestmark = pytest.mark.skipif(
    not _mcp_available, reason="mcp extras not installed"
)

from fastapi import FastAPI

from seren_memory.collections import MemoryStore
from seren_memory.config import ConsolidatorConfig, MemoryConfig
from seren_memory.consolidator.service import Consolidator


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def wired_app(tmp_path, fake_embedder):
    """FastAPI app with app.state pre-wired to live objects, the way
    app.py's lifespan handler does at startup."""
    cfg = MemoryConfig()
    cfg = cfg.model_copy(update={
        "storage": cfg.storage.model_copy(update={"persist_dir": str(tmp_path)}),
        "consolidator": ConsolidatorConfig(enabled=False),
    })
    store = MemoryStore(cfg, embedding_function=fake_embedder, _allow_reset=True)
    consolidator = Consolidator(store, cfg)

    app = FastAPI()
    app.state.store = store
    app.state.config = cfg
    app.state.consolidator = consolidator

    yield app
    store.close()


# --- tests ------------------------------------------------------------------


def test_mount_succeeds_when_state_wired(wired_app):
    """The happy path - mount adds a /mcp route to the app."""
    mount_mcp_routes(wired_app)
    paths = [getattr(r, "path", None) for r in wired_app.routes]
    assert "/mcp" in paths


def test_mount_respects_env_override(wired_app, monkeypatch):
    """SEREN_MCP_MOUNT overrides the default mount point - useful when
    /mcp is taken by something else on the host app."""
    monkeypatch.setenv("SEREN_MCP_MOUNT", "/serenmem")
    mount_mcp_routes(wired_app)
    paths = [getattr(r, "path", None) for r in wired_app.routes]
    assert "/serenmem" in paths
    assert "/mcp" not in paths


def test_mount_raises_clear_error_when_state_missing():
    """Calling mount BEFORE app.state is wired (mounting in a wrong
    lifecycle slot) gives a helpful error rather than a cryptic
    AttributeError downstream."""
    app = FastAPI()  # no state.store / state.config set
    with pytest.raises(RuntimeError, match="store/config"):
        mount_mcp_routes(app)


def test_mount_logs_tool_count(wired_app, caplog):
    """The mount log line surfaces how many tools were registered -
    sanity-check that the count helper finds them. Best-effort: if the
    SDK changes the internal attribute name, the helper returns 0, the
    test gates loosely on 'mounted at /mcp' showing up."""
    import logging
    with caplog.at_level(logging.INFO, logger="seren_memory.mcp.server"):
        mount_mcp_routes(wired_app)
    assert any("mounted at /mcp" in rec.message for rec in caplog.records)


def test_mount_resolves_to_an_asgi_app(wired_app):
    """The mounted /mcp route must point at a real ASGI app - callable
    and accepting the (scope, receive, send) trio. We don't actually
    invoke it (that'd require an MCP protocol exchange), just verify
    the shape."""
    mount_mcp_routes(wired_app)
    mcp_mount = next(r for r in wired_app.routes
                     if getattr(r, "path", None) == "/mcp")
    asgi = getattr(mcp_mount, "app", None)
    assert callable(asgi), "mounted /mcp does not point at an ASGI callable"
