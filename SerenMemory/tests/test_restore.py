"""
Memory puts a snapshot back: at startup, into an empty store, with a reason,
and what was purged since stays purged (seren_memory.restore).

No route and no tool restores anything; it is two
config keys and a restart, and a store that holds something is never
overwritten.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from seren_memory.config import BackupConfig, ConsolidatorConfig, MemoryConfig, StorageConfig
from seren_memory.restore import restore_if_asked
from seren_sinew.stores import RestoreRefused, pending_tombstones


def _core(client, text):
    did = client.post("/drafts", json={"operations": [{"kind": "new_core", "content": text, "topic": "t"}]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]["long_term_id"]


def test_a_new_box_comes_up_with_the_old_memories_and_without_what_was_purged(make_client, tmp_path):
    # the old box: two cores, a snapshot, then one of them is purged and a later snapshot records it
    old = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                                   backup=BackupConfig(dir=str(tmp_path / "stash"), every_hours=0)))
    kept = _core(old, "a promise, kept word for word.")
    leak = _core(old, "a leaked key")
    first = old.post("/stores/snapshot").json()["snapshot"]
    old.post(f"/long/{leak}/purge", json={"reason": "a secret", "purge_backups": False})
    old.post("/stores/snapshot")

    # the new box: an empty store, asked to restore the OLDER snapshot
    cfg = MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                       storage=StorageConfig(persist_dir=str(tmp_path / "new" / "chroma")),
                       backup=BackupConfig(dir=str(tmp_path / "new" / "backups"), every_hours=0,
                                           restore_from=first["path"], restore_reason="moving to the cluster"))
    said = []
    rep = restore_if_asked(cfg, log=said.append)
    assert rep["restored"] and rep["snapshot"] == first["id"] and rep["tombstones_to_replay"] == 1
    assert [t["id"] for t in pending_tombstones(tmp_path / "new" / "backups" / "seren-memory")] == [leak]
    new = make_client(cfg)
    store = new.app.state.store
    assert store.get_by_id(kept)["content"] == "a promise, kept word for word."
    assert store.get_by_id(leak) is None, "purged after the snapshot: it does not come back"
    assert pending_tombstones(tmp_path / "new" / "backups" / "seren-memory") == []
    assert new.post("/search", json={"query": "promise kept word for word"}).json()["hits"], "and it can be recalled"
    # the key still in the config at the next start: passed by
    again = restore_if_asked(cfg, log=said.append)
    assert again["restored"] is False and "already restored" in again["why"]


def test_it_is_refused_without_a_reason_and_never_lands_on_a_store_that_holds_something(make_client, tmp_path):
    old = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                                   backup=BackupConfig(dir=str(tmp_path / "stash"), every_hours=0)))
    _core(old, "a promise, kept word for word.")
    snap = old.post("/stores/snapshot").json()["snapshot"]
    cfg = MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                       storage=StorageConfig(persist_dir=str(tmp_path / "new" / "chroma")),
                       backup=BackupConfig(dir=str(tmp_path / "new" / "backups"), every_hours=0, restore_from=snap["path"]))
    with pytest.raises(RestoreRefused, match="asked for with a reason"):
        restore_if_asked(cfg)
    assert not list(Path(cfg.resolved_persist_dir()).iterdir())
    # the live store of the old box, asked to roll back onto itself: nothing happens
    live = old.app.state.config.model_copy(deep=True)
    live.backup.restore_from, live.backup.restore_reason = snap["path"], "a rollback"
    rep = restore_if_asked(live)
    assert rep["restored"] is False and rep["not_empty"] == ["memory"]
    assert restore_if_asked(MemoryConfig()) is None, "nothing asked, nothing done"
    for path in ("/stores/restore", f"/stores/snapshots/{snap['id']}/restore"):
        assert old.post(path).status_code in (404, 405), "and there is still no route"
