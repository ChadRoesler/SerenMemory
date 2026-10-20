"""
Pictures kept with memories (metadata `eidetic`, the three tools,
remember's `keep`, the /memory/{id}/eidetic routes, and where the pictures
go when the entry moves). 15 Oct 2026.

Pinned here:
- a picture is kept WITH an entry: the bytes under their hash in the store's
  own folder, the row on the entry; recall says how many, look_at_memory
  shows them as image content
- the pictures go where the entry goes: promoted by hand, drafted into a core
  (union of the sources'), attached (the core grows them), superseded (the
  new core takes them forward, the old keeps its own), archived to pruned
- the bytes are swept only when no entry in any tier names them; a purge
  sweeps at once, and the tombstone counts it
- the store reaches only the shelves eidetic.sources names; what is kept is
  decided by the bytes; the memory is written whatever happens to the pictures
- the routes, and the snapshot: the folder rides by hash, a rehearsal looks
  for every named picture, a new box gets them back
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seren_memory.app import create_app
from seren_memory.collections import MemoryStore
from seren_memory.config import BackupConfig, EideticConfig, MemoryConfig, StorageConfig
from seren_memory.models.schemas import Draft, DraftOperation, DraftOpKind, ShortTermEntry
from seren_memory.restore import eidetic_store, restore_if_asked

PNG = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 100
JPG = b"\xff\xd8\xff\xe0" + b"\x01" * 100


def _gallery(tmp_path: Path) -> Path:
    g = tmp_path / "gallery"
    g.mkdir(exist_ok=True)
    (g / "lantern.png").write_bytes(PNG)
    (g / "houseboat.jpg").write_bytes(JPG)
    (g / "notes.txt").write_text("words are the memory")
    return g


def _cfg(tmp_path: Path, sources=None, **backup) -> MemoryConfig:
    return MemoryConfig(storage=StorageConfig(persist_dir=str(tmp_path / "store" / "chroma")),
                        backup=BackupConfig(every_hours=0, **backup),
                        eidetic=EideticConfig(dir=str(tmp_path / "store" / "eidetic"),
                                                  sources=[str(s) for s in (sources or [])]))


@pytest.fixture
def store(tmp_path, fake_embedder):
    cfg = _cfg(tmp_path, sources=[_gallery(tmp_path)])
    s = MemoryStore(cfg, embedding_function=fake_embedder, _allow_reset=True, eidetic=eidetic_store(cfg))
    yield s
    s.close()


@pytest.fixture
def tools(store):
    pytest.importorskip("mcp")
    from seren_memory.mcp.tools import MemoryToolImpl
    return MemoryToolImpl(store, store._config)


# ── kept with the entry ─────────────────────────────────────────────────────

def test_a_picture_is_kept_with_a_memory_and_shown_back(tools, tmp_path):
    from mcp.server.fastmcp import Image
    g = tmp_path / "gallery"
    r = tools.remember("the lantern, reaching", topic="art", keep=[str(g / "lantern.png"), str(g / "houseboat.jpg")])
    assert r["ok"] and len(r["kept"]) == 2 and r["refused"] == []
    a, b = r["kept"]
    assert a["mime"] == "image/png" and a["file"] == f"eidetic-{a['digest'][:8]}.png" and a["source"].endswith("lantern.png")
    folder = tmp_path / "store" / "eidetic"
    assert (folder / a["digest"][:2] / a["digest"]).read_bytes() == PNG
    assert not any(p.suffix for p in folder.rglob("*") if p.is_file()), "no filename says what a picture is"

    got = tools.get_memory(r["id"])
    assert [k["digest"] for k in got["metadata"]["eidetic"]] == [a["digest"], b["digest"]], "the rows ride on the entry"
    assert tools.recall("lantern")["hits"][0]["eidetic"] == 2, "a hit says how many"
    assert tools.what_do_you_remember()["entries"][0]["eidetic"] == 2
    look = tools.look_at_memory(r["id"])
    assert isinstance(look, list) and look[0]["shown"] == 2 and look[0]["tier"] == "short"
    assert [type(x) for x in look[1:]] == [Image, Image] and look[1].data == PNG
    assert tools.look_at_memory("nope")["ok"] is False

    r2 = tools.keep_with_memory(r["id"], str(g / "notes.txt"))
    assert not r2["ok"] and "words are the memory" in r2["refused"][0]["why"]
    assert tools.drop_from_memory(r["id"], b["digest"])["ok"] and not tools.drop_from_memory(r["id"], b["digest"])["ok"]
    assert tools.store.eidetic.has(b["digest"]), "within the grace period the bytes stay"
    assert tools.store.sweep_eidetic(grace=0) == [b["digest"]]
    assert len(tools.get_memory(r["id"])["metadata"]["eidetic"]) == 1


def test_only_the_shelves_the_config_names(tools, tmp_path):
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(PNG)
    r = tools.remember("still written", keep=[str(outside), "https://example.test/a.png", str(tmp_path / "gallery" / "nope.png")])
    assert r["ok"] and tools.get_memory(r["id"])["content"] == "still written"
    whys = [x["why"] for x in r["refused"]]
    assert any("not under a folder the sources allow" in w for w in whys)
    assert any("no URL prefixes are configured" in w for w in whys)
    assert any(w.startswith("no file at") for w in whys)
    assert r["kept"] == [] and tools.store.eidetic.count() == 0


# ── the pictures go where the entry goes ────────────────────────────────────

def test_promoted_by_hand_the_picture_moves_to_the_core(tools, tmp_path):
    r = tools.remember("one", keep=[str(tmp_path / "gallery" / "lantern.png")])
    d = r["kept"][0]["digest"]
    long_id = tools.promote_memory_now(r["id"])["long_term_id"]
    core = tools.get_memory(long_id)
    assert core["tier"] == "long" and core["metadata"]["eidetic"][0]["digest"] == d
    assert tools.store.sweep_eidetic(grace=0) == [], "named by the core: kept"
    assert tools.look_at_memory(long_id)[0]["shown"] == 1


def _submit(store, ops):
    d = Draft(summary="drafted", operations=[DraftOperation(**o) for o in ops])
    return store.submit_draft(d)


def test_drafted_into_a_core_the_pictures_follow_through_every_operation(store, tmp_path):
    g = tmp_path / "gallery"
    one = store.add_short(ShortTermEntry(content="saw the lantern"))
    two = store.add_short(ShortTermEntry(content="saw the houseboat"))
    ka = store.keep_with(one.id, g / "lantern.png")
    kb = store.keep_with(two.id, g / "houseboat.jpg")
    # NEW_CORE from both: the union
    d = _submit(store, [{"kind": DraftOpKind.NEW_CORE, "content": "the pictures of that night",
                         "source_short_ids": [one.id, two.id], "topic": "art"}])
    rep = store.review_draft(d.id, [{"op": 0, "verdict": "approve"}])
    core_id = rep["applied"][0]["long_term_id"] if "applied" in rep else store.get_long_all()[0]["id"]
    core = store.get_by_id(core_id)
    assert {k["digest"] for k in store.eidetic_in(core["metadata"])} == {ka["digest"], kb["digest"]}
    assert store.get_by_id(one.id)["tier"] == "pruned", "the sources are archived, pictures still named"
    assert store.sweep_eidetic(grace=0) == []
    # ATTACH a new episode with a third picture: the satellite has it, and the core grows it
    (g / "third.png").write_bytes(PNG[:-1] + b"\x01")
    three = store.add_short(ShortTermEntry(content="a third"))
    kc = store.keep_with(three.id, g / "third.png")
    d = _submit(store, [{"kind": DraftOpKind.ATTACH, "content": "and a third", "source_short_ids": [three.id],
                         "target_core_id": core_id}])
    store.review_draft(d.id, [{"op": 0, "verdict": "approve"}])
    core = store.get_by_id(core_id)
    assert kc["digest"] in {k["digest"] for k in store.eidetic_in(core["metadata"])}
    (sat,) = store.satellites_of(core_id)
    assert [k["digest"] for k in store.eidetic_in(sat["metadata"])] == [kc["digest"]]
    # SUPERSEDE: the new core takes them forward, the old keeps its own
    four = store.add_short(ShortTermEntry(content="rethought"))
    d = _submit(store, [{"kind": DraftOpKind.SUPERSEDE, "content": "that night, rethought", "source_short_ids": [four.id],
                         "target_core_id": core_id}])
    rep = store.review_draft(d.id, [{"op": 0, "verdict": "approve"}])
    new_id = next(r["id"] for r in store.get_long_all() if r["metadata"].get("supersedes") == core_id)
    assert {k["digest"] for k in store.eidetic_in(store.get_by_id(new_id)["metadata"])} == {ka["digest"], kb["digest"], kc["digest"]}
    assert len(store.eidetic_in(store.get_by_id(core_id)["metadata"])) == 3
    # a purge of the new core: its pictures stay while the old core and the pruned sources still name them
    store.purge_long(new_id, "test", purge_backups=False)
    assert store.eidetic.count() == 3
    # purge the old core too: its satellite and its archived sources go in the cascade, and nothing names them now
    tomb = store.purge_long(core_id, "test", purge_backups=False)
    assert tomb["cascade"]["eidetic"] == 3 and store.eidetic.count() == 0


def test_a_purge_sweeps_at_once(store, tmp_path):
    s = store.add_short(ShortTermEntry(content="x"))
    k = store.keep_with(s.id, tmp_path / "gallery" / "lantern.png")
    long_id = store.promote_short_to_long(s.id)
    tomb = store.purge_long(long_id, "a secret in the picture", purge_backups=False)
    assert tomb["cascade"]["eidetic"] == 1 and not store.eidetic.has(k["digest"])


def test_over_mcp_the_look_is_a_text_block_and_image_blocks(tools, tmp_path):
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ImageContent, TextContent
    from seren_memory.mcp.tools import register_tools
    r = tools.remember("a bird", keep=[str(tmp_path / "gallery" / "lantern.png")])
    mcp = FastMCP("t")
    register_tools(mcp, tools.store, tools.config)
    got = asyncio.run(mcp.call_tool("look_at_memory", {"memory_id": r["id"]}))
    blocks = got[0] if isinstance(got, tuple) else got
    assert [type(b) for b in blocks] == [TextContent, ImageContent] and blocks[1].mimeType == "image/png"
    assert f"eidetic-{r['kept'][0]['digest'][:8]}.png" in blocks[0].text


# ── over HTTP, and in the snapshot ──────────────────────────────────────────

def test_the_routes_and_the_snapshot(tmp_path, fake_embedder):
    from seren_sinew.stores import verify_snapshot
    g = _gallery(tmp_path)
    cfg = _cfg(tmp_path, sources=[g])
    with TestClient(create_app(cfg, embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        sid = c.post("/short", json={"content": "the secret picture"}).json()["id"]
        r = c.post(f"/memory/{sid}/eidetic", json={"source": str(g / "lantern.png"), "caption": "for the margins"})
        assert r.status_code == 200
        k = r.json()["eidetic"]
        assert c.post("/memory/nope/eidetic", json={"source": str(g / "lantern.png")}).status_code == 404
        assert c.post(f"/memory/{sid}/eidetic", json={"source": str(tmp_path / "x.png")}).status_code == 400
        b = c.get(f"/memory/{sid}/eidetic/{k['digest']}")
        assert b.status_code == 200 and b.content == PNG and b.headers["content-type"] == "image/png"
        assert b.headers["x-content-type-options"] == "nosniff" and b.headers["content-security-policy"].startswith("sandbox")
        assert c.get(f"/memory/{sid}/eidetic/{'0' * 64}").status_code == 404
        assert c.get(f"/memory/{sid}").json()["metadata"]["eidetic"][0]["file"] == k["file"]

        snap = c.post("/stores/snapshot").json()["snapshot"]
        man = json.loads((Path(snap["path"]) / "manifest.json").read_text())
        assert man["stores"] == ["memory", "eidetic"] and man["eidetic"] == {"count": 1, "bytes": len(PNG)}
        assert f"raw/eidetic/{k['digest'][:2]}/{k['digest']}" in [f["path"] for f in man["files"]]
        assert "lantern" not in json.dumps(man["files"]) and "margins" not in json.dumps(man)
        snap2 = c.post("/stores/snapshot").json()["snapshot"]
        a = Path(snap["path"]) / "raw" / "eidetic" / k["digest"][:2] / k["digest"]
        assert os.path.samefile(a, Path(snap2["path"]) / "raw" / "eidetic" / k["digest"][:2] / k["digest"])
        assert verify_snapshot(Path(snap2["path"])) == []
        rep = c.post(f"/stores/snapshots/{snap2['id']}/rehearse").json()
        assert rep["ok"], rep
        man_path = Path(snap["path"]) / "manifest.json"
        man["files"] = [f for f in man["files"] if not f["path"].endswith(k["digest"])]
        man_path.write_text(json.dumps(man))
        a.unlink()
        rep = c.post(f"/stores/snapshots/{snap['id']}/rehearse").json()
        assert not rep["ok"] and any("1 of the 1 eidetic picture(s)" in p for p in rep["problems"]), rep
        assert c.delete(f"/memory/{sid}/eidetic/{k['digest']}").status_code == 200
        assert c.delete(f"/memory/{sid}/eidetic/{k['digest']}").status_code == 404

    new = MemoryConfig(storage=StorageConfig(persist_dir=str(tmp_path / "new" / "chroma")),
                       eidetic=EideticConfig(dir=str(tmp_path / "new" / "eidetic")),
                       backup=BackupConfig(every_hours=0, restore_from=snap2["path"], restore_reason="the old box died"))
    assert restore_if_asked(new)["restored"]                 # __main__ does this before the store opens
    with TestClient(create_app(new, embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        got = c.get(f"/memory/{sid}").json()
        assert got["metadata"]["eidetic"][0]["caption"] == "for the margins"
        assert c.get(f"/memory/{sid}/eidetic/{k['digest']}").content == PNG


def test_a_snapshot_from_before_there_were_eidetic_still_restores(tmp_path, fake_embedder):
    cfg = _cfg(tmp_path)
    with TestClient(create_app(cfg, embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        c.post("/short", json={"content": "from before"})
        snap = c.post("/stores/snapshot").json()["snapshot"]
        assert json.loads((Path(snap["path"]) / "manifest.json").read_text())["stores"] == ["memory"]
    new = MemoryConfig(storage=StorageConfig(persist_dir=str(tmp_path / "new" / "chroma")),
                       eidetic=EideticConfig(dir=str(tmp_path / "new" / "eidetic")),
                       backup=BackupConfig(every_hours=0, restore_from=snap["path"], restore_reason="moving"))
    said: list[str] = []
    assert restore_if_asked(new, log=said.append)["restored"] and any("holds no eidetic" in m for m in said)
    with TestClient(create_app(new, embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        assert c.get("/short").json()["entries"][0]["content"] == "from before"


# ── the viewer ──────────────────────────────────────────────────────────────

def test_the_halls_show_eidetic_pictures_and_the_backup_panel(tmp_path, fake_embedder):
    with TestClient(create_app(_cfg(tmp_path), embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        page = c.get("/viewer").text
        assert "eideticStrip" in page and "/eidetic/" in page, "an entry's pictures are shown, fetched through the token"
        assert "renderBackups" in page and "api('/stores')" in page, "the same backup panel every Seren viewer has"
        root = c.get("/").json()
        assert root["eidetic"] == {"count": 0, "bytes": 0}, "a count and a size for the overview"


# ── the Eidetic hall: picture first (19 Oct 2026) ─────────────────────────────

def test_the_gallery_is_one_row_per_picture_with_everything_it_is_attached_to(store, tmp_path):
    """Chad: 'a gallery of images, then when clicked shows what its attached to'. A
    picture rides on more than one memory by design, so the gallery is keyed by it."""
    g = tmp_path / "gallery"
    one = store.add_short(ShortTermEntry(content="saw the lantern"))
    ka = store.keep_with(one.id, g / "lantern.png", caption="the lantern")
    d = _submit(store, [{"kind": DraftOpKind.NEW_CORE, "content": "the night of the lantern",
                         "source_short_ids": [one.id], "topic": "art"}])
    store.review_draft(d.id, [{"op": 0, "verdict": "approve"}])
    core_id = next(r["id"] for r in store.get_long_all())
    two = store.add_short(ShortTermEntry(content="and the houseboat"))
    kb = store.keep_with(two.id, g / "houseboat.jpg")
    d = _submit(store, [{"kind": DraftOpKind.ATTACH, "content": "the houseboat, that same night",
                         "source_short_ids": [two.id], "target_core_id": core_id}])
    store.review_draft(d.id, [{"op": 0, "verdict": "approve"}])

    pics = {p["digest"]: p for p in store.eidetic_gallery()}
    assert set(pics) == {ka["digest"], kb["digest"]}, "one row per picture, not per entry"
    lantern, boat = pics[ka["digest"]], pics[kb["digest"]]
    assert [e["id"] for e in lantern["entries"]] == [core_id], "the archived short is not shown by default"
    assert lantern["caption"] == "the lantern" and lantern["via"] == core_id and not lantern["missing"]
    sat = next(e for e in boat["entries"] if e["metadata"].get("kind") == "satellite")
    assert {e["id"] for e in boat["entries"]} == {core_id, sat["id"]}, "the satellite AND the core it grew"
    assert sat["core"] == {"id": core_id, "content": "the night of the lantern"}, "a satellite brings its core"
    assert "eidetic" not in sat["metadata"], "the picture is the subject; its row is not repeated"
    archived = {p["digest"]: p for p in store.eidetic_gallery(include_pruned=True)}
    assert {e["tier"] for e in archived[ka["digest"]]["entries"]} == {"long", "pruned"}
    assert archived[ka["digest"]]["via"] == core_id, "the bytes are fetched through a live entry first"


def test_the_gallery_route_names_what_to_fetch_and_says_when_bytes_are_gone(tmp_path, fake_embedder):
    g = _gallery(tmp_path)
    cfg = _cfg(tmp_path, sources=[g])
    with TestClient(create_app(cfg, embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        assert c.get("/eidetic").json() == {"ok": True, "enabled": True, "count": 0, "bytes": 0, "pictures": []}
        sid = c.post("/short", json={"content": "the lantern"}).json()["id"]
        k = c.post(f"/memory/{sid}/eidetic", json={"source": str(g / "lantern.png")}).json()["eidetic"]
        sid2 = c.post("/short", json={"content": "the houseboat"}).json()["id"]
        c.post(f"/memory/{sid2}/eidetic", json={"source": str(g / "houseboat.jpg")})
        data = c.get("/eidetic").json()
        assert data["count"] == 2 and data["bytes"] == len(PNG) + len(JPG)
        assert data["pictures"][0]["entries"][0]["content"] == "the houseboat", "the newest kept first"
        lantern = next(p for p in data["pictures"] if p["digest"] == k["digest"])
        assert c.get(f"/memory/{lantern['via']}/eidetic/{lantern['digest']}").content == PNG
        assert len(c.get("/eidetic", params={"limit": 1}).json()["pictures"]) == 1
        for f in Path(cfg.eidetic.dir).rglob(k["digest"] + "*"):
            f.unlink()
        assert next(p for p in c.get("/eidetic").json()["pictures"] if p["digest"] == k["digest"])["missing"]


def test_the_viewer_has_an_eidetic_hall(tmp_path, fake_embedder):
    with TestClient(create_app(_cfg(tmp_path), embedding_function=fake_embedder, _allow_store_reset=True)) as c:
        page = c.get("/viewer").text
        assert 'data-tab="eidetic"' in page and "loadEidetic" in page and "/eidetic?include_pruned=" in page
        assert "openPicture" in page and "attached to" in page, "open a picture, see what it is attached to"
