"""

Proves the security-relevant invariants so the [corp] path can pass review:

  CONFIG
    - default is OFF (a normal box never changes TLS behavior)
    - yaml `tls.trust_system_store: true` flips it on
    - SEREN_MEMORY_TRUST_SYSTEM_STORE env override works (truthy + falsy)

  INJECTION (the security-load-bearing part)
    - OFF  -> truststore is NEVER imported, inject_into_ssl NEVER called,
              and nothing is logged (no silent global TLS change)
    - ON + truststore present -> inject_into_ssl() called exactly once, logged
    - ON + truststore MISSING -> no crash; clear "[corp]" guidance logged;
              certifi behavior preserved (no injection)
    - injection is logged every time it happens (never silent - the
      anti-spooky-action invariant)

These are unit tests with truststore mocked, so they run on any box (CI
included) without a real corporate proxy. The live behavior against an actual
intercepting proxy is a manual smoke test on the corp box itself.
"""
from __future__ import annotations

import sys
import types

import pytest

from seren_memory.config import MemoryConfig, load_config
from seren_memory.__main__ import _maybe_inject_truststore, _force_utf8_stdio


# ── helpers ─────────────────────────────────────────────────────────────────

class _Recorder:
    """Captures log lines passed to the injected log= callable."""
    def __init__(self):
        self.lines: list[str] = []
    def __call__(self, msg):
        self.lines.append(msg)
    @property
    def text(self):
        return "\n".join(self.lines)


@pytest.fixture(autouse=True)
def _clean_truststore(monkeypatch):
    """Ensure each test controls whether `truststore` is importable. Remove any
    real/leftover module so the missing-case is deterministic."""
    monkeypatch.delitem(sys.modules, "truststore", raising=False)
    for k in ("AGENT", "SEREN_MEMORY_TRUST_SYSTEM_STORE"):
        monkeypatch.delenv(k, raising=False)
    yield


def _install_fake_truststore(monkeypatch):
    """Inject a fake `truststore` module that records inject_into_ssl() calls."""
    fake = types.ModuleType("truststore")
    calls = {"n": 0}
    fake.inject_into_ssl = lambda: calls.__setitem__("n", calls["n"] + 1)
    monkeypatch.setitem(sys.modules, "truststore", fake)
    return calls


def _block_truststore(monkeypatch):
    """Force `import truststore` to raise ImportError."""
    import builtins
    real_import = builtins.__import__
    def fake_import(name, *a, **k):
        if name == "truststore":
            raise ImportError("No module named 'truststore'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)


# ── CONFIG ──────────────────────────────────────────────────────────────────

def test_default_is_off():
    """A fresh config must NOT touch the trust store. Security default."""
    assert MemoryConfig().tls.trust_system_store is False


def test_yaml_flips_it_on(tmp_path, monkeypatch):
    p = tmp_path / "seren-memory.yaml"
    p.write_text("tls:\n  trust_system_store: true\n")
    monkeypatch.setenv("SEREN_MEMORY_CONFIG", str(p))
    assert load_config().tls.trust_system_store is True


def test_yaml_absent_tls_block_defaults_off(tmp_path, monkeypatch):
    p = tmp_path / "seren-memory.yaml"
    p.write_text("server:\n  port: 7420\n")  # no tls: block at all
    monkeypatch.setenv("SEREN_MEMORY_CONFIG", str(p))
    assert load_config().tls.trust_system_store is False


@pytest.mark.parametrize("val,expect", [
    ("1", True), ("true", True), ("YES", True), ("on", True),
    ("0", False), ("false", False), ("no", False), ("", False),
])
def test_env_override(tmp_path, monkeypatch, val, expect):
    # empty string is falsy in the loader's `if v :=` guard, so "" stays default-off
    monkeypatch.setenv("SEREN_MEMORY_CONFIG", str(tmp_path / "none.yaml"))
    if val:
        monkeypatch.setenv("SEREN_MEMORY_TRUST_SYSTEM_STORE", val)
    assert load_config().tls.trust_system_store is expect


# ── INJECTION: OFF path (the most security-critical) ────────────────────────

def test_off_never_imports_truststore(monkeypatch):
    """When the flag is OFF, truststore must NEVER be imported and inject must
    NEVER run. This is the 'a normal box is untouched' guarantee."""
    # Make ANY import of truststore explode - if the code touches it, we fail.
    _block_truststore(monkeypatch)
    rec = _Recorder()
    cfg = MemoryConfig()  # default off
    _maybe_inject_truststore(cfg, log=rec)   # must not raise
    assert rec.lines == [], "OFF path must be completely silent"


def test_off_is_silent_even_if_truststore_present(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)
    rec = _Recorder()
    _maybe_inject_truststore(MemoryConfig(), log=rec)
    assert calls["n"] == 0, "inject must NOT be called when flag is off"
    assert rec.lines == []


# ── INJECTION: ON + present ─────────────────────────────────────────────────

def test_on_with_truststore_injects_once_and_logs(monkeypatch):
    calls = _install_fake_truststore(monkeypatch)
    rec = _Recorder()
    cfg = MemoryConfig(tls={"trust_system_store": True})
    _maybe_inject_truststore(cfg, log=rec)
    assert calls["n"] == 1, "inject_into_ssl must be called exactly once"
    assert any("OS trust store" in l for l in rec.lines), \
        "injection must be logged (anti-spooky-action invariant)"


def test_on_injection_is_never_silent(monkeypatch):
    """Every injection MUST log. A silent global ssl-module rewrite is exactly
    what we refuse - the log is how a future debugger sees why TLS changed."""
    _install_fake_truststore(monkeypatch)
    rec = _Recorder()
    _maybe_inject_truststore(MemoryConfig(tls={"trust_system_store": True}), log=rec)
    assert len(rec.lines) >= 1


# ── INJECTION: ON + missing (the Jackie chicken-and-egg) ────────────────────

def test_on_without_truststore_does_not_crash(monkeypatch):
    """Flag on but [corp] not installed: must NOT raise. Startup continues."""
    _block_truststore(monkeypatch)
    rec = _Recorder()
    cfg = MemoryConfig(tls={"trust_system_store": True})
    _maybe_inject_truststore(cfg, log=rec)  # must not raise

def test_on_without_truststore_gives_corp_guidance(monkeypatch):
    _block_truststore(monkeypatch)
    rec = _Recorder()
    _maybe_inject_truststore(MemoryConfig(tls={"trust_system_store": True}), log=rec)
    assert "seren-memory[corp]" in rec.text, \
        "must tell the operator exactly which extra to install"
    assert "continuing" in rec.text.lower(), \
        "must signal it's degrading, not dying"


# ── UTF-8 backstop (the consolidator-crash fix) ─────────────────────────────

def test_force_utf8_stdio_is_idempotent_and_safe():
    """Calling it must never raise, even multiple times, even when stdout
    doesn't support reconfigure (pytest captures it)."""
    _force_utf8_stdio()
    _force_utf8_stdio()  # twice = still fine


def test_force_utf8_stdio_swallows_non_reconfigurable(monkeypatch):
    """A stream without .reconfigure (older Python / wrapped sink) must be a
    no-op, not a crash."""
    class Dumb:
        pass
    monkeypatch.setattr(sys, "stdout", Dumb())
    monkeypatch.setattr(sys, "stderr", Dumb())
    _force_utf8_stdio()  # AttributeError must be swallowed