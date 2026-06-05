"""Tests for the no-MCP and mount-failure fallback paths in
seren_memory.app.

The conditional `try/except ImportError` in app.py's lifespan is what
makes [mcp] extras optional — when the mcp SDK isn't installed, the
import raises, we catch, log a quiet line, and continue in HTTP-only
mode. A second `except Exception` catches mount failures that aren't
ImportError (transport API drift, mount path collision, etc.) and logs
loudly but keeps the HTTP API up.

We simulate both paths via monkeypatch.setattr — replacing
`seren_memory.mcp.server.mount_mcp_routes` with a function that raises
the right exception type. The lifespan's `from .mcp.server import
mount_mcp_routes` picks up the patched version, calls it, and lands in
the matching except branch. Same observable behaviour as the actual
missing-SDK or actual-mount-failure case, with zero sys.modules churn
that could leak into adjacent tests.

The existing test_drafts.py / test_smoke.py / etc. implicitly verify
the no-MCP path works (they run without mcp in their dev deps and
don't break). This file is the EXPLICIT contract — so a future change
that accidentally lets an exception escape will be caught here loudly.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from seren_memory.app import create_app
from seren_memory.config import ConsolidatorConfig, MemoryConfig


@pytest.fixture
def cfg_for_tmp(tmp_path):
    """A MemoryConfig with the storage path forced to a per-test tmp,
    and the background consolidator disabled (we don't need it ticking
    during these lifecycle tests)."""
    cfg = MemoryConfig()
    return cfg.model_copy(update={
        "storage": cfg.storage.model_copy(update={"persist_dir": str(tmp_path)}),
        "consolidator": ConsolidatorConfig(enabled=False),
    })


# ─── tests ──────────────────────────────────────────────────────────────────


def test_missing_extras_logs_http_only_and_skips_mcp_mount(
        cfg_for_tmp, fake_embedder, monkeypatch, capsys):
    """Verify that when mcp is absent (or simulated absent), the lifespan
    catches the ImportError and falls back to HTTP-only mode.

    When mcp IS installed we simulate absence via monkeypatch so we don't
    trash sys.modules. When mcp is NOT installed the ImportError fires
    naturally in the lifespan — no patching required. Either way we land
    in the same except-ImportError branch and observe the same behaviour."""
    try:
        import seren_memory.mcp.server as srv_mod

        # mcp is installed in this env — simulate absence by patching.
        def _simulated_missing_import(app):
            raise ImportError("simulated: mcp package not installed")

        monkeypatch.setattr(srv_mod, "mount_mcp_routes", _simulated_missing_import)
    except ImportError:
        # mcp genuinely absent — the lifespan ImportError fires naturally.
        pass

    app = create_app(cfg_for_tmp, embedding_function=fake_embedder,
                     _allow_store_reset=True)
    with TestClient(app) as tc:
        captured = capsys.readouterr()
        assert "MCP extras not installed; HTTP-only mode" in captured.out

        # /mcp must NOT be in routes.
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/mcp" not in paths

        # HTTP API still works — sanity check.
        r = tc.get("/health")
        assert r.status_code == 200


def test_mount_failure_logs_loudly_but_keeps_http_up(
        cfg_for_tmp, fake_embedder, monkeypatch, capsys):
    """Simulate the second except branch: mcp IS installed (import
    succeeds) but mount_mcp_routes raises a non-ImportError (e.g.
    transport API drift, app-state bug). The lifespan should log
    loudly + continue.

    Requires mcp to be installed — can't trigger a mount failure if the
    import itself never succeeds."""
    pytest.importorskip("mcp")

    import seren_memory.mcp.server as srv_mod

    def _simulated_mount_failure(app):
        raise RuntimeError("synthetic mount failure for test")
    monkeypatch.setattr(srv_mod, "mount_mcp_routes", _simulated_mount_failure)

    app = create_app(cfg_for_tmp, embedding_function=fake_embedder,
                     _allow_store_reset=True)
    with TestClient(app) as tc:
        captured = capsys.readouterr()
        assert "MCP mount failed" in captured.out
        assert "synthetic mount failure" in captured.out

        # Still no /mcp route (mount didn't complete).
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/mcp" not in paths

        # HTTP API still up — the whole point of the broad except.
        r = tc.get("/health")
        assert r.status_code == 200


def test_mcp_extras_installed_mounts_route(
        cfg_for_tmp, fake_embedder, capsys):
    """Positive sanity check: when mcp IS installed and no patching is
    in play, the real mount_mcp_routes runs and /mcp lands in the route
    table. The fallback paths above don't fire."""
    pytest.importorskip("mcp")

    app = create_app(cfg_for_tmp, embedding_function=fake_embedder,
                     _allow_store_reset=True)
    with TestClient(app) as tc:
        captured = capsys.readouterr()
        # Neither fallback message should appear when mount succeeds.
        assert "MCP extras not installed" not in captured.out
        assert "MCP mount failed" not in captured.out

        # /mcp IS in the route table.
        paths = [getattr(r, "path", None) for r in app.routes]
        assert "/mcp" in paths