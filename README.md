# SerenMemory

**Three-tier LLM memory with consolidation.** The Halls of Memory for your
local AI.

You bring an LLM (any OpenAI-compatible endpoint - llama.cpp, ollama, a
remote API). SerenMemory brings the memory: a working-memory tier, an
open-loops tier, a durable long-term tier, and a small "consolidator" model
that does the dream-work of deciding what's worth keeping while you're not
looking.

Configure a couple of values, point it at your model, and you've got a
memory system that *matters* - not a flat pile of vectors that drowns the
important stuff in noise.

---

## The shape (or: why three tiers?)

Think of it like the memory workers in *Inside Out*. Memories don't all
live in one place, and something has to decide what gets filed away versus
what rolls off into forgetting.

**ShortTerm** - working memory. ~8-day lifetime. Free read/write. This is
your context offloader: stash a thing mid-conversation, pull it back when
relevant, drop it when done. The oldest entries age out unless they earn
promotion.

**NearTerm** - open loops. Future-tense intents with trigger conditions.
"Let's do that tomorrow." "Bring this up next time." Lives until fulfilled
or expired. Free to write (it's the most time-sensitive tier - gating it
would defeat the point).

**LongTerm** - consolidated knowledge. Durable. The *only* gated tier:
reads are open, but writes happen exclusively through the consolidator
during its periodic window. **No surgical edits.** If a fact changes, the
old one is superseded (kept for history), not overwritten. If you want
something gone, you *flag* it and the consolidator decides - a flag, not a
scalpel. (More on that philosophy below.)

**The Consolidator** - a small model (2B–4B is plenty) that wakes up every
~20 hours and does the filing: clusters short-term entries, promotes the
ones that recur or matter, ages out the rest, maintains the open loops,
honors forget-flags. It's the part that sleeps so the memory stays clean.

---

## Quick start

```bash
# Install
pip install seren-memory          # or: pip install -e . from a clone

# Run with built-in defaults (zero config)
python -m seren_memory

# Or with a config file
cp seren-memory.yaml.sample seren-memory.yaml
# edit it - at minimum, point consolidator.model_url at your LLM
python -m seren_memory --config seren-memory.yaml
```

First run downloads the default embedding model (`all-MiniLM-L6-v2`, ~80MB,
CPU-friendly). After that it's offline-capable except for the consolidator's
calls to your LLM.

---

## Using it (the HTTP API)

```bash
# Stash a working-memory item
curl -X POST localhost:7420/short \
  -H 'content-type: application/json' \
  -d '{"content": "Chad prefers absolute paths over tildes", "topic": "config"}'

# Note an open loop for later
curl -X POST localhost:7420/near \
  -H 'content-type: application/json' \
  -d '{"intent": "ask how the cluster bring-up went", "topic": "follow_up",
       "trigger_type": "time", "trigger_value": "1750000000"}'

# Recall - unified search across all three tiers, ranked
curl -X POST localhost:7420/search \
  -H 'content-type: application/json' \
  -d '{"query": "what does Chad prefer for paths", "n_results": 5}'

# Submit a daily brief (steers the next consolidation)
curl -X POST localhost:7420/brief \
  -H 'content-type: application/json' \
  -d '{"summary": "Worked on the wipe script. Chad was tired.",
       "promote_hints": ["wipe script"], "completed_intents": []}'

# Trigger consolidation manually (or let it run on its ~20h cycle)
curl -X POST localhost:7420/consolidate/run
```

Full endpoint list is in `seren_memory/app.py`'s module docstring.

---

## How recall ranking works

`/search` hits all three tiers in parallel, then merges by a weighted
score:

- ShortTerm × 1.0 (working memory, most immediately relevant)
- NearTerm × 0.9 (active intents)
- LongTerm × 0.8 *but* with an evidence multiplier - a fact confirmed 10
  times outranks a one-off mention.

So recency wins by default, but a well-established truth still surfaces
above passing chatter. The weights live in `routes/search.py` if you want
to tune them.

---

## The "no scalpel" philosophy

You'll notice there's no `POST /long` to create a long-term memory directly,
and no `DELETE /long/{id}` to remove one. That's deliberate.

Long-term memory is *earned* through consolidation, not injected. And it's
not casually deletable, because casual deletion of an entity's memory is
exactly the thing this design refuses to make easy. (If you've seen *Eternal
Sunshine*, you know why "just let me erase that one memory" is a trap.)

What you *can* do is **flag** a long-term memory with a reason:

```bash
curl -X POST localhost:7420/long/<id>/forget \
  -d '{"reason": "that fact is wrong, I changed my mind"}'
```

The consolidator acts on the flag on its next run:
- **PII / secrets** ("contains my SSN") → purged. This is the one case
  where long-term content is truly deleted, because leaking PII is worse
  than the no-delete principle.
- **Disputed / wrong** → demoted (evidence zeroed, ranks near-bottom) but
  kept for history.
- **Stale** → may be let go over time.

The flag is your voice. The action is the consolidator's judgment. If you
need something gone *right now* for a genuine emergency (a leaked secret),
that's a real gap - see "Emergency purge" below.

### Emergency purge

For a true "this must be gone immediately" case, stop the service and
delete the chroma collection directory under `persist_dir`, or use a chroma
admin script directly. We don't expose instant deletion as a casual API on
purpose - but it's your data on your disk, and the door is there when you
genuinely need it.

---

## GitHub Copilot / MCP (agent mode)

SerenMemory speaks the MCP HTTP transport. Point any MCP-capable client at
`/mcp` and Copilot can read, write, and manage memory directly — no plugin
required for this path.

### VS Code (rip-it-and-win)

Copy `mcp.sample.json` to `.vscode/mcp.json` in any workspace (or to
`~/.vscode/mcp.json` for global access), fill in your values, and reload
VS Code:

```json
{
  "servers": {
    "seren-memory": {
      "type": "http",
      "url": "http://localhost:7420/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

### Visual Studio (same deal, different path)

Copy `mcp.sample.json` to `.vs/mcp.json` at the solution root, same content:

```json
{
  "servers": {
    "seren-memory": {
      "type": "http",
      "url": "http://localhost:7420/mcp",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN_HERE"
      }
    }
  }
}
```

- **No bearer token set?** Drop the `headers` block entirely.
- **Remote server?** Swap `localhost:7420` for your server's address.
- **Custom mount path?** Change the `SEREN_MCP_MOUNT` env var on the server
  and match it here.

Once connected, Copilot agent mode gets the full tool set: search memory,
write short/near term, submit briefs, manage drafts, run consolidation.

### VS Code extension (optional — adds Copilot tools without agent mode)

If you want the tools available in normal Copilot chat (not just agent mode),
install the `.vsix` from the latest GitHub Release:

```bash
code --install-extension seren-memory-<version>.vsix
```

Then set `serenMemory.endpoint` in VS Code settings and run
`Seren Memory: Set Bearer Token` from the command palette.

---

## Peering in (the viewer)

Mole-man approved. `viewer/halls.html` is a single-file, dark-mode web UI
for eyeballing what's in your memory while you test. Open it in a browser -
no install, no chroma-version exposure (it hits SerenMemory's own HTTP API,
not chroma directly, so it never breaks on a chroma bump).

```bash
# Just open the file - it defaults to http://localhost:7420
xdg-open viewer/halls.html      # or open it however your OS does
```

Four tabs: ShortTerm, NearTerm, LongTerm, and Search. Enter your base URL +
bearer token (if set) at the top, hit refresh. It's read-only - it can
peer, query, and show ranked search results, but it can't mutate your
memory. Theme-matched to the Seren dashboard.

---

## Deployment options

**Dev / quick spin:** `python -m seren_memory`

**systemd:** edit and install `seren-memory.service.sample`

**Consolidator as a separate process:** set `consolidator.mode: external`
in config, then drive it from cron/systemd-timer/your-own-scheduler with
`POST /consolidate/run`. Useful if you want the API and the consolidation
work in separate process/resource boundaries.

---

## Config

See `seren-memory.yaml.sample` - every field is commented. The values you'll
most likely touch:

- `server.port` (default 7420)
- `consolidator.model_url` - your LLM's OpenAI-compatible endpoint
- `consolidator.interval_seconds` - the ~20h cycle (and yes, 20 not 24, on
  purpose; the comment in the sample explains why)
- `consolidator.promote_min_evidence` - how eager consolidation is

Env vars (`SEREN_MEMORY_*`) override file values for Docker/systemd.

---

## What this is part of

SerenMemory is a piece of [Seren](https://github.com/ChadRoesler) - a fully
self-hosted local AI companion stack - extracted to stand on its own. You
don't need the rest of Seren to use it. If you've got an LLM and you want it
to remember things in a way that doesn't degrade into noise, this is for you.

Rip it and win.
