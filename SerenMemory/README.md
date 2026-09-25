# SerenMemory

**Three-tier LLM memory with consolidation.** The Halls of Memory for your
local AI.

You bring an LLM (any OpenAI-compatible endpoint - llama.cpp, ollama, a
remote API). SerenMemory brings the memory: a working-memory tier, an
open-loops tier and a durable long-term tier. Its companion,
**SerenHippocampus**, is the small model that does the dream-work of deciding
what's worth keeping while you're not looking.

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
reads are open, but writes happen only when the main model approves what the
hippocampus drafted during a sleep. **No surgical edits.** If a fact changes, the
old one is superseded (kept for history), not overwritten. If you want
something gone, you *flag* it and the next sleep purges it - a flag, not a
scalpel. (More on that philosophy below.)

**The hippocampus** - a separate service, SerenHippocampus, with a small
model (2B-4B is plenty). A brief from the main model opens each sleep; it
reads the short-terms, drafts what should become long-term, and the main model
approves or denies each proposal. It also ages out the rest, maintains the
open loops and executes forget-flags. It's the part that sleeps so the memory
stays clean. Memory holds the data and applies what is approved; it runs no
model of its own.

---

## Quick start

```bash
# Install
pip install seren-memory          # or: pip install -e . from a clone

# Run with built-in defaults (zero config)
python -m seren_memory

# Or with a config file
cp seren-memory.yaml.sample seren-memory.yaml
python -m seren_memory --config seren-memory.yaml
```

First run downloads the default embedding model (`all-MiniLM-L6-v2` as ONNX,
~80MB, CPU-friendly, no torch). After that it's fully offline. Naming a different `storage.embedding_model`
needs `pip install seren-memory[st]` (sentence-transformers, and torch with
it); the installer's `--st` flag does that.

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

# Submit a daily brief (it opens the hippocampus's next sleep)
curl -X POST localhost:7420/brief \
  -H 'content-type: application/json' \
  -d '{"summary": "Worked on the wipe script. Chad was tired.",
       "promote_hints": ["wipe script"], "completed_intents": []}'
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

Long-term memory is *earned* through a sleep, not injected. And it's
not casually deletable, because casual deletion of an entity's memory is
exactly the thing this design refuses to make easy. (If you've seen *Eternal
Sunshine*, you know why "just let me erase that one memory" is a trap.)

What you *can* do is **flag** a long-term memory with a reason:

```bash
curl -X POST localhost:7420/long/<id>/forget \
  -d '{"reason": "that fact is wrong, I changed my mind"}'
```

The flag means *purge this*: the hippocampus purges the entry on its next
tick and leaves a tombstone (id, reason, time - never the content), cascading
to its satellites, its source short-terms in the pruned tier, any draft that
named it, and migration backups. It exists for the emergency - "I pasted you
my SSH key and it got committed to memory" - and it is deliberate: a flag is a
decision, not a hint.

**Demotion is a different thing and it is not deletion.** When a newer memory
overrides an older one ("I like blue", then a month later "I like yellow"),
the old one is kept and demoted: it ranks below, carries `superseded_by`, and
recall can still say "last week you said blue, now it's yellow". Both are
valid; one supersedes the other. That is a `supersede` operation in a draft,
triggered by the newer memory, never by the forget route.

The flag is your voice. The purge is the hippocampus's.

### Emergency purge

For a true "this must be gone immediately" case, `POST /long/{id}/purge`
(or the `purge_memory_now` MCP tool) executes the flag now instead of at the
next tick - same cascade, same tombstone. It is still not a delete button:
there is no route that removes a memory without leaving the tombstone.

---

## The hippocampus: drafts and the review

The sleep cycle lives in its own service, **SerenHippocampus** (port 7424).
It holds no store: it reads short-terms from here, writes **drafts** here,
resubmits what the reviewer denied, and purges what was flagged. Memory
keeps the data and applies what is approved.

A draft is a list of operations on long-term - `new_core`, `attach` (a
satellite on an existing core; the core's evidence grows, its wording may be
restated), `supersede` (the old core stays, demoted, still recallable through
history), `verbatim` - each reviewed on its own:

| route                          | who        | what                                          |
|--------------------------------|------------|-----------------------------------------------|
| `POST /drafts`                 | hippocampus| submit a draft                                |
| `GET /drafts?status=pending`   | reviewer   | the queue (also the `list_drafts` MCP tool)   |
| `GET /drafts/{id}`, `/chain`   | reviewer   | one draft with every verdict; every attempt in its chain (`get_draft`) |
| `POST /drafts/{id}/review`     | reviewer   | `{"decisions": [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "deny", "critique": "..."}]}` (`review_draft`) |
| `POST /drafts/{id}/close`      | hippocampus | the cull: a reviewed draft whose chain has landed is closed; it leaves the reviewed queue (409 while pending) |
| `POST /brief/{id}/consume`     | hippocampus | the brief that opened the sleep is retired once the chain lands; `GET /brief` shows open briefs only unless `include_consumed=true` |
| `GET /long/{id}/satellites`    | anyone     | a core's surroundings                         |
| `POST /tidy`                   | hippocampus| age out, maintain near-term, sweep, purge flagged |
| `POST /long/{id}/purge`        | hippocampus, or the model in an emergency | execute a purge with the cascade; `GET /tombstones` |

Long-term is a core and its surroundings: recall returns cores; satellites
(`kind: satellite`, `core_id`) and superseded cores come back only when asked
(`include_satellites`, `include_superseded`).
Every core hit from `/search` (and the `recall` tool) carries `surroundings`:
how many satellites stand behind it, when the latest landed, what it
superseded and what superseded it; `with_surroundings: true` brings the most
recent satellites and the superseded core along in full. Having the
surroundings is one thing; a hit that does not say they exist is how they
stay unread.

Until 25 Sept 2026 these were called *dockets*. In Probe and the Corpus
Callosum a docket is the briefing packet a search hands back, so here the
word is draft. `/dockets/*` still answers as an unadvertised alias for one
release, so a hippocampus built before the rename keeps working until it is
upgraded; the store renames its `seren_dockets` collection to `seren_drafts`
in place on first boot.

**The in-process consolidator is retired** (25 Sept 2026). Memory used to run
its own one-draft-per-cluster loop in a thread; that loop, `/consolidate/run`,
`/consolidate/wake`, `/consolidator/status`, the old `/drafts/{id}/approve`
queue and the `consolidate_now` / `prepare_consolidation` family of MCP tools
are gone. Its collections (`seren_consolidator_drafts`,
`seren_consolidator_runs`) are left on disk untouched; the old drafts are
still scrubbed by a purge. A `consolidator:` block in an existing config still
loads: `enabled` and `mode` are ignored, `pruned_safety_days` is read by
`/tidy`.

---

## GitHub Copilot / MCP (agent mode)

SerenMemory speaks the MCP HTTP transport. Point any MCP-capable client at
`/mcp` and Copilot can read, write, and manage memory directly - no plugin
required for this path.

### VS Code (rip-it-and-win)

Put this in `.vscode/mcp.json` in any workspace (or `~/.vscode/mcp.json`
for global access), fill in your values, and reload VS Code:

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

Put the same block in `.vs/mcp.json` at the solution root:

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
write short/near term, submit briefs, review the hippocampus's drafts.

### VS Code extension (optional - adds Copilot tools without agent mode)

If you want the tools available in normal Copilot chat (not just agent mode),
install the `.vsix` from the latest GitHub Release:

```bash
code --install-extension seren-memory-<version>.vsix
```

Then set `serenMemory.endpoint` in VS Code settings and run
`Seren Memory: Set Bearer Token` from the command palette.

---

## Peering in (the viewer)

Mole-man approved. The running service serves its own viewer at
`http://localhost:7420/viewer` - dark-mode, single page, shipped inside the
package (`seren_memory/viewer/ui/`), so there is nothing to open from disk and
it can never disagree with the chroma version underneath it (it talks to
SerenMemory's HTTP API, never to chroma). `/viewer` is public; the API calls
it makes carry your bearer token if you set one.

Tabs for each tier, search, the hippocampus's drafts (every operation and
its verdict), and the migration modal when the embedder changes. It is
read-only: it can peer, query and show ranked recall, and never deletes or
reviews - the review is the main model's.

---

## Deployment options

**Dev / quick spin:** `python -m seren_memory`

**systemd:** edit and install `seren-memory.service.sample`

**The sleep:** install SerenHippocampus beside it (Starwright wires it to
this Memory's url and bearer).

---

## Config

See `seren-memory.yaml.sample` - every field is commented. The values you'll
most likely touch:

- `server.port` (default 7420)
- `lifetimes.short_term_seconds` - how long a short-term lives unpromoted
- `consolidator.pruned_safety_days` - how long an aged-out short-term waits in
  the pruned tier before `/tidy` sweeps it (0 = no safety net)

The model, the schedule and how eager a sleep is are the hippocampus's config.

Env vars (`SEREN_MEMORY_*`) override file values for Docker/systemd.

---

## Implementation architecture

### Three-tier storage

Memory entries live in a ChromaDB collection, one per tier, under a local persist directory. Each entry stores `content`, `topic`, `timestamp`, and tier-specific metadata (evidence count for long-term, trigger conditions for near-term).

- **Short-term** — free read/write. Entries age out after ~8 days unless a draft consumes them. The hippocampus groups them by exact topic tag.
- **Near-term** — open intents with trigger conditions (time-based or topic-based). Fulfilled intents are promoted to long-term as a record.
- **Long-term** — gated writes. Entries are created only by an approved draft operation. Evidence count tracks how many times a fact has been confirmed across briefs; higher evidence → higher recall rank.

### The sleep

Run by SerenHippocampus, not by this process:

1. **Brief** — the main model's brief opens the sleep; its promote and noise hints steer the draft.
2. **Draft** — the hippocampus proposes operations (`new_core`, `attach`, `supersede`, `verbatim`) and submits them to `POST /drafts`.
3. **Review** — the main model approves or denies each; denied ones come back redrafted with the critique, until a terminal attempt.
4. **Tidy** — `POST /tidy` ages out short-terms, maintains near-term, sweeps the pruned tier and purges what is flagged.
5. **Close** — once the chain lands, the hippocampus closes the drafts and consumes the brief.

### Recall ranking

`/search` hits all three tiers in parallel, then merges by a weighted score:

- ShortTerm × 1.0 (working memory, most immediately relevant)
- NearTerm × 0.9 (active intents)
- LongTerm × 0.8 *but* with an evidence multiplier — a fact confirmed 10 times outranks a one-off mention.

The weights live in `routes/search.py` if you want to tune them.

---

## Tests

| Test file | What it covers |
|-----------|----------------|
| `tests/test_search.py`, `test_by_topic.py` | Unified recall and the association edge (exact topic-tag match) |
| `tests/test_drafts.py`, `test_brief.py` | The hippocampus's drafts: per-operation review, satellites, supersession, the purge cascade, the close, the `/dockets` alias; briefs |
| `tests/test_store_hygiene.py` | The chroma boundary: cosine everywhere, crash-safe rebuilds, distilled re-embed on migration, metadata keys that can be removed |
| `tests/test_embedder.py` | The embedder stamp, the boot guard, migration and its restore |
| `tests/test_mcp_*.py` | The MCP mount, tools and fallback |
| `tests/test_auth.py`, `test_server_block.py`, `test_exposure_is_wired.py`, `test_tls.py` | The shared Meninges surface: bearer, config block, open-LAN refusal, corp TLS |
| `tests/test_mcp_tools.py` | MCP tool surface for search/write/brief/draft review |

```bash
pytest tests/
```

---

## What this is part of

SerenMemory is a piece of [Seren](https://github.com/ChadRoesler) - a fully
self-hosted local AI companion stack - extracted to stand on its own. You
don't need the rest of Seren to use it. If you've got an LLM and you want it
to remember things in a way that doesn't degrade into noise, this is for you.

Rip it and win.
