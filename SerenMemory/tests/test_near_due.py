"""
What is due (29 Oct 2026): the near tier read by its triggers, not by
similarity.

- a time intent past its moment is due, most overdue first; one still ahead
  is waiting with how long; an event intent waits on its match string; an
  always intent is standing
- completed intents are history and appear nowhere; expired ones are their
  own bucket, never acted on
- ISO dates are read as moments; a time intent whose value is not a time
  waits with a note instead of quietly never coming due
- the route and the MCP tool agree, and `say` reads the whole in a line
"""
from __future__ import annotations

import pytest

from seren_memory import due
from seren_memory.config import ConsolidatorConfig, MemoryConfig

NOW = 1_800_000_000.0


def _rows():
    def r(i, kind, value=None, created=NOW - 100, completed=False, expires=None, content="x"):
        return {"id": i, "content": content, "metadata": {"trigger_type": kind, "trigger_value": value,
                                                          "created_at": created, "completed": completed,
                                                          "expires_at": expires, "topic": "t"}}
    return [
        r("late-2d", "time", str(NOW - 2 * 86400), content="two days overdue"),
        r("late-1h", "time", NOW - 3600, content="an hour overdue"),
        r("soon", "time", NOW + 1800, content="in half an hour"),
        r("iso", "time", "2027-06-01T00:00:00", content="an ISO moment"),      # NOW is 15 Jan 2027
        r("unreadable", "time", "whenever", content="no readable time"),
        r("game-night", "event", "mentions:board-game", content="ask for the stories"),
        r("standing-old", "always", created=NOW - 500, content="a standing note"),
        r("standing-new", "always", created=NOW - 10, content="a newer standing note"),
        r("done", "time", NOW - 99999, completed=True, content="acted on"),
        r("stale", "always", expires=NOW - 60, content="past its expiry"),
    ]


def test_intents_are_bucketed_by_their_triggers():
    b = due.bucket(_rows(), now=NOW)
    assert [x["id"] for x in b["due"]] == ["late-2d", "late-1h"], "most overdue first"
    assert b["due"][0]["overdue"] == 2 * 86400 and b["due"][1]["due_at"] == NOW - 3600
    assert [x["id"] for x in b["standing"]] == ["standing-new", "standing-old"], "newest standing note first"
    waiting = {x["id"]: x for x in b["waiting"]}
    assert [x["id"] for x in b["waiting"]][:2] == ["soon", "iso"], "soonest first, then the event and the unreadable"
    assert waiting["soon"]["due_in"] == 1800 and waiting["iso"]["due_at"] > NOW
    assert waiting["game-night"]["waits_for"] == "mentions:board-game" and "sort" not in waiting["game-night"]
    assert "no readable time" in waiting["unreadable"]["note"]
    assert [x["id"] for x in b["expired"]] == ["stale"] and b["expired"][0]["expired_for"] == 60
    everything = [x["id"] for k in ("due", "standing", "waiting", "expired") for x in b[k]]
    assert "done" not in everything, "completed is history"
    assert b["say"] == "2 due, 2 standing, 4 waiting (the next in 30 min), 1 expired."
    assert due.bucket([], now=NOW)["say"] == "Nothing is due and nothing is waiting."


def test_a_moment_is_read_from_numbers_strings_and_iso_dates_only():
    assert due._when(5) == 5.0 and due._when("5.5") == 5.5 and due._when("2027-06-01") > NOW
    assert due._when(None) is None and due._when(True) is None and due._when("") is None and due._when("soon") is None


@pytest.fixture
def client(make_client):
    return make_client(MemoryConfig(consolidator=ConsolidatorConfig(pruned_safety_days=0)))


def test_the_route_reads_the_store_and_the_clock(client):
    client.post("/near", json={"intent": "ask the user for the lamp story", "trigger_type": "time",
                               "trigger_value": str(NOW - 60)})
    client.post("/near", json={"intent": "a standing note", "trigger_type": "always"})
    client.post("/near", json={"intent": "when the board game comes up", "trigger_type": "event",
                               "trigger_value": "mentions:board-game"})
    r = client.get("/near/due", params={"now": NOW}).json()
    assert [x["intent"] for x in r["due"]] == ["ask the user for the lamp story"] and r["due"][0]["overdue"] == 60
    assert [x["intent"] for x in r["standing"]] == ["a standing note"]
    assert r["waiting"][0]["waits_for"] == "mentions:board-game" and r["say"] == "1 due, 1 standing, 1 waiting."
    done = r["due"][0]["id"]
    client.post(f"/near/{done}/complete")
    assert client.get("/near/due", params={"now": NOW}).json()["due"] == [], "completed is history"
    # the MCP tool asks the same clock: tests/test_mcp_tools.py::test_what_is_due_reads_the_triggers
