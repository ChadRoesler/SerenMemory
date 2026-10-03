"""
Memory says what it keeps and snapshots it (seren_sinew.stores).

2 Oct 2026: everything a brain is sat in one folder with nothing copying it.
A snapshot is the raw store plus a plain export; it is taken on the service's
own schedule, so a standalone install is covered. Pinned here:

- GET /stores declares the store; POST /stores/snapshot takes one
- the export is every tier as text and metadata, with the tombstones
- the manifest carries the embedder and the counts
- backup.enabled: false turns it off, and the routes say so
- no route restores a snapshot or deletes one
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from seren_memory.config import BackupConfig, ConsolidatorConfig, MemoryConfig


@pytest.fixture
def client(make_client, tmp_path_factory):
    # its own snapshots folder: the default is beside the store, and every
    # test's store shares one parent here
    bk = tmp_path_factory.mktemp("backups")
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                                    backup=BackupConfig(dir=str(bk), every_hours=0)))


def test_the_default_place_is_beside_the_store(make_client):
    c = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False), backup=BackupConfig(every_hours=0)))
    cfg = c.app.state.config
    assert cfg.resolved_backup_dir() == cfg.resolved_persist_dir().parent / "backups"
    assert Path(c.get("/stores").json()["snapshots"]["dir"]) == cfg.resolved_backup_dir() / "seren-memory"


def _core(client, text):
    did = client.post("/drafts", json={"operations": [{"kind": "new_core", "content": text, "topic": "t"}]}).json()["id"]
    return client.post(f"/drafts/{did}/review", json={"decisions": [{"op": 0, "verdict": "approve"}]}).json()["results"][0]["long_term_id"]


def test_it_says_what_it_keeps(client):
    d = client.get("/stores").json()
    assert d["ok"] and d["service"] == "seren-memory"
    s = d["stores"][0]
    assert (s["name"], s["kind"], s["backed_up"], s["exists"]) == ("memory", "chroma", True, True)
    assert Path(d["snapshots"]["dir"]).name == "seren-memory"
    assert d["snapshots"]["count"] == 0 and d["snapshots"]["keep_daily"] == 14


def test_a_snapshot_holds_the_raw_store_and_a_plain_export(client):
    client.post("/short", json={"content": "the model runs on port 7200", "topic": "model"})
    core = _core(client, "the user's lantern promise.")
    gone = _core(client, "a leaked key")
    client.post(f"/long/{gone}/purge", json={"reason": "a secret", "purge_backups": False})

    r = client.post("/stores/snapshot", json={"reason": "before the embedder cut-over"})
    assert r.status_code == 200, r.text
    snap = r.json()["snapshot"]
    root = Path(snap["path"])
    man = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert man["reason"] == "before the embedder cut-over" and man["service"] == "seren-memory"
    assert man["embedder"] == (client.app.state.config.storage.embedding_model or "all-MiniLM-L6-v2 (default)")
    assert man["counts"]["short"] == 1 and man["counts"]["long"] == 1 and man["version"]
    assert (root / "raw" / "memory" / "chroma.sqlite3").is_file(), "the store as it sits on disk"

    def rows(name):
        return [json.loads(x) for x in (root / "export" / name).read_text(encoding="utf-8").splitlines()]
    assert [x["content"] for x in rows("short.jsonl")] == ["the model runs on port 7200"]
    longs = rows("long.jsonl")
    assert [x["id"] for x in longs] == [core] and longs[0]["metadata"]["kind"] == "core"
    assert "a leaked key" not in (root / "export" / "long.jsonl").read_text(encoding="utf-8"), "purged before the snapshot: not in it"
    tombs = rows("tombstones.jsonl")
    assert [t["id"] for t in tombs] == [gone] and "a leaked key" not in json.dumps(tombs), "the tombstone rides along, without the content"
    assert man["exports"]["drafts.jsonl"] == 2

    listed = client.get("/stores/snapshots").json()
    assert listed["count"] == 1 and listed["snapshots"][0]["id"] == snap["id"]
    assert client.get("/stores").json()["snapshots"]["latest"]["id"] == snap["id"]


def test_switched_off_the_routes_say_so(make_client):
    c = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False), backup=BackupConfig(enabled=False)))
    assert c.get("/stores").status_code == 404 and "backup.enabled" in c.get("/stores").json()["error"]
    assert c.post("/stores/snapshot").status_code == 404


def test_backup_dir_can_be_another_disk(make_client, tmp_path):
    c = make_client(MemoryConfig(consolidator=ConsolidatorConfig(enabled=False),
                                 backup=BackupConfig(dir=str(tmp_path / "elsewhere"), every_hours=0)))
    snap = c.post("/stores/snapshot").json()["snapshot"]
    assert Path(snap["path"]).parent == (tmp_path / "elsewhere" / "seren-memory").resolve()


def test_no_route_restores_or_deletes_a_snapshot(client):
    sid = client.post("/stores/snapshot").json()["snapshot"]["id"]
    for path in ("/stores/restore", f"/stores/snapshots/{sid}", f"/stores/snapshots/{sid}/restore"):
        assert client.post(path).status_code in (404, 405)
        assert client.delete(path).status_code in (404, 405)
    assert client.get("/stores/snapshots").json()["count"] == 1
