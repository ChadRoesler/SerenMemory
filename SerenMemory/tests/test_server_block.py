"""The `server:` block goes through seren-meninges, so the family's rule is
Memory's rule: loopback when the operator said nothing, an explicit host
honoured, a null host treated as unset.

Memory used to spell its own `host: str = "0.0.0.0"`, so a yaml with no
host line put every memory on the LAN with no token and nothing looked
broken. These pin the yaml path specifically - the class default is easy to
read, the loader is where the answer actually comes from.
"""
from __future__ import annotations

from seren_memory.config import MemoryConfig, load_config


def _write(tmp_path, text):
    p = tmp_path / "seren-memory.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_no_yaml_is_loopback(tmp_path):
    cfg = load_config(str(tmp_path / "absent.yaml"))
    assert (cfg.server.host, cfg.server.port) == ("127.0.0.1", 7420)


def test_a_yaml_with_no_host_line_is_loopback(tmp_path):
    cfg = load_config(_write(tmp_path, "server:\n  port: 7420\n"))
    assert cfg.server.host == "127.0.0.1"


def test_an_explicit_lan_bind_is_honoured(tmp_path):
    """Widening is a thing you did."""
    cfg = load_config(_write(tmp_path, "server:\n  host: 0.0.0.0\n"))
    assert cfg.server.host == "0.0.0.0"


def test_a_null_host_is_unset_not_the_string_None(tmp_path):
    cfg = load_config(_write(tmp_path, "server:\n  host:\n  port: 7420\n"))
    assert cfg.server.host == "127.0.0.1"


def test_the_env_override_still_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("SEREN_MEMORY_HOST", "10.0.0.5")
    cfg = load_config(_write(tmp_path, "server:\n  port: 7420\n"))
    assert cfg.server.host == "10.0.0.5"


def test_the_in_code_default_agrees_with_the_loader():
    assert MemoryConfig().server.host == "127.0.0.1"
