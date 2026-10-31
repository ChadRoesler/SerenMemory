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
  -d '{"content": "The user prefers absolute paths over tildes", "topic": "config"}'

# Note an open loop for later
curl -X POST localhost:7420/near \
  -H 'content-type: application/json' \
  -d '{"intent": "ask how the cluster bring-up went", "topic": "follow_up",
       "trigger_type": "time", "trigger_value": "1750000000"}'

# What is due - the open loops by their triggers, not by similarity: due now
# (most overdue first), standing notes, waiting (and on what), expired
curl localhost:7420/near/due

# Recall - unified search across all three tiers, ranked
curl -X POST localhost:7420/search \
  -H 'content-type: application/json' \
  -d '{"query": "what does the user prefer for paths", "n_results": 5}'

# Submit a daily brief (it opens the hippocampus's next sleep)
curl -X POST localhost:7420/brief \
  -H 'content-type: application/json' \
  -d '{"summary": "Worked on the wipe script. Late night, short session.",
       "promote_hints": ["wipe script"], "completed_intents": []}'
```

Full endpoint list is in `seren_memory/app.py`'s module docstring.

---

## Eidetic: pictures kept with memories

A picture can *be* the memory: what was drawn at 3 a.m., what the screen
showed. `remember(..., keep=[...])` keeps one as the entry is written,
`keep_with_memory` adds one to an entry in any recall tier, `look_at_memory`
hands them back as image content, `drop_from_memory` takes one off. The
bytes go in a `eidetic/` folder beside the store under their sha256 and
nothing else (`seren_sinew.blobs`); the row (caption, source, type) rides on
the entry's metadata under `eidetic`. Both are in the same snapshot, the
folder hardlinked from the previous one.

The pictures go where the entry goes. Promoted by hand, they move to the
core. Drafted by the hippocampus, a core made from several episodes carries
the union of their pictures; a satellite carries its own, and the core it
attaches to grows them; a superseding core takes the old core's forward
while the old keeps its own. Archived to pruned, they wait out the safety
window with the entry. The bytes are swept only when no entry in any tier
names them, and a purge sweeps at once.

Where an eidetic picture may be taken **from** is the `eidetic:` block's to say:
folders on this box (the Workbench gallery `make_image` saves to) and URL
prefixes. Nothing listed means nothing can be kept, and the refusal says so.

```yaml
eidetic:
  sources:
    - ~/seren/stores/workbench/gallery
    - https://
```

---

## How recall ranking works

`/search` hits all three tiers in parallel, then merges by one score
(`seren_memory/ranking.py`): the hit's cosine **similarity**, plus small
additive nudges:

- NearTerm +0.05: at *equal* relevance a promise comes first
- ShortTerm +0.02: then working memory; LongTerm +0
- LongTerm also gets ln(evidence_count) × 0.015: a core confirmed ten times
  wins a near tie, never a contest

Relevance decides; the nudges only settle ties. The multipliers this used
to run on (1/(1+distance) × a tier weight × an evidence multiplier) squeezed
every real difference in relevance into a band a 20% tier weight could
overrule, and short-term entries crowded intents and long cores out of the
top 50 (29 Oct 2026; the module's docstring has the measurements). The old
shape is kept behind `ranking.MODE = "inverse"` for side-by-side runs in
Probe's harness, not as a setting to run on.

**A core is as near as its nearest satellite.** A satellite is an episode,
worded the way a question is worded, so it is often the nearest row in the
store. A satellite hit counts for its core: the core takes the satellite's
distance when that is the better one and carries `matched_via`, the episode
that found it. History never lifts (the pull, below).

**Low core orbit** (30 Oct 2026). When a short-term memory is written, the
store looks once at the cores and marks the short as *orbiting* each core
within `orbit.max_distance` (`orbit: [core ids]`, `orbit_distances` on the
short's metadata). It is a hint, not an attach: nothing is written to
long-term and nothing is reviewed. In recall a short that matches the
question brings the cores it orbits up with it the way a satellite does,
`matched_via` naming the short (`tier: short`), so the durable context
arrives with the fresh one in one pass. In the review view every source
short's orbit is shown beside the operation (`orbit`), and `orbit_check`
says when the operation goes against it: an attach aimed at a core the
episode does not orbit, a new core whose source orbits an existing one, an
age-out letting a connected short go, an episode in two orbits that should
be a shared satellite. On a supersede the orbit is re-tested against the
replacing core, the way the satellites are, and moves or breaks: never
inherited. On a forget it is released; the short stays. A short in orbit
still ages out like any other. The threshold is the whole game: too loose
and every short lifts something; measure it in Probe's harness before
moving it (`orbit.enabled`, `orbit.max_distance`, `orbit.max_cores`).
`POST /short/reorbit` marks every short's orbit again from the cores as
they are now: for a store from before orbits, a seeded store whose shorts
landed before its cores, or a changed threshold.

**Strength** (`ranking.STRENGTH`, off until measured). The Boy Scout
fire: a core with a lot behind it should win a *vague* question. The term
is what the store already knows beyond the evidence count: ln(1 + its
satellites) × `satellites`, ln(1 + the shorts in orbit around it) × `orbit`,
and a `warmth` that fades over `warm_days` from `last_confirmed`; summed and
capped at `cap` (0.04), so it settles ties among near-equal matches and
never crosses a real gap (strength that beat relevance is exactly what
buried the current title under eleven satellite-heavy cores on 20 Oct). All
three weights ship at 0; Probe's harness sets them in-process
(`--memory-strength=satellites:0.01,orbit:0.01,cap:0.03`) and the numbers
decide what, if anything, ships.

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
the old one is kept and demoted: it becomes a satellite of the core that
replaced it, carries `superseded_by`, and recall can still say "last week you
said blue, now it's yellow". Both are valid; one supersedes the other. That is
a `supersede` operation in a draft, triggered by the newer memory, never by
the forget route.

A superseded core also loses its pull (29 Oct 2026). Its satellites move
under the new core, and each is tested against it: one at least as near to
the new core as it was to the old still supports what is true now and keeps
its pull (`carry: true`; it may lift the new core in recall), one that was
about the old state hangs on as history with a broken pull (`carry: false`,
never lifts, the distances kept as `pull_old` / `pull_new`). The demoted core
itself is always history. Measured before it was built: history that lifted
dragged the current core into every question about the past. The shorts in
orbit around the old core get the same test (`orbits_moved` /
`orbits_released` on the supersede's result): one still as near to the new
core moves its orbit there; one that was about the old state is a plain
short again. An orbit is never inherited.

The flag is your voice. The purge is the hippocampus's.

**A core that was reworded wrongly can go back, and that is gated too.** An
approved `attach` may restate its core (below), and the wording it replaced
is kept on the core as `restated_from`. If the rewording was a mistake, the
model flags the core with the `undo_restate` MCP tool and a reason; the
hippocampus puts the earlier wording back on its next tick, and the replaced
text, the reason and the time stay on the core (`restate_undone`,
`restate_undone_reason`, `restate_undone_at`). It can only return a core to
words a review already approved. There is **no HTTP route** for it, on
purpose: like delete, it is not something a person holding the token gets to
do to someone's memory. The model asks; the hippocampus acts; the record
stays.

**A satellite can find another home, through the same gate.** A satellite
can hang on more than one core, and it no longer has to stay where it was
born. When a review leaves a core new or newly worded, the review view shows
`nearby_satellites` (the existing episodes that sit closest to it, with the
cores they hang on), and the approval can carry `link_satellite_ids` to hang
them under it as well. Afterwards, the `link_satellite` and
`unlink_satellite` MCP tools flag a change with a reason, and the hippocampus
carries it out on its next tick; a move is a link and an unlink. A satellite
always keeps at least one core, a link is evidence for the core it adds and
an unlink takes that back, and every change is logged on the satellite
(`relinks`). `where_else` (read-only) shows a satellite's own cores against
the nearest strangers, or a core's nearby satellites. Similarity only puts
candidates in front of the model; nothing is linked by a threshold, because
in a store about one person everything sits near everything else.

**Pruned is not lost, and the trail reads both ways.** A short-term that
leaves for the pruned archive now says why in its metadata: the operation
that consumed it and what it became (`consumed_by_draft`, `consumed_as`,
`consumed_into`, `consumed_satellites`), an age-out (`aged_out`), or a
retraction (`retracted`). `get_memory` adds a `pruned_because` sentence for
it. In the review view, a denied operation whose chain has another attempt
shows `redrafted_in`: the operation that carries it now, with its status and
where it landed. The `retract_memory` MCP tool takes back a short-term the
model wrote (a duplicate, or a mistake) with a reason, into the archive. It
refuses long-term entries (use forget), entries a pending draft holds, and
pinned ones. A restatement that echoes the hippocampus's own core list (the
"(N episode(s) already attached)" count, or the core cut off at "...") is
refused as a rewording, the same way as one that drops most of the core.

**The same episode kept twice becomes one.** `where_else` on a satellite
shows its `look_alikes`: satellites with the same words, or near-identical
ones. `fold_satellite` flags one to fold into the other, with a reason. On
the next tick the survivor hangs on every core either one hung on, a core
that counted both gives back the double count, the pictures come along, and
the folded one's date, cores, reason, and words (when they differ) stay in
the survivor's `folds`. A core's earlier wording (`was_core`) is history and
is never folded away. A review's `note` is kept once, on the draft
(`extra.review_note`); an operation keeps only a note written for it.

**The Map.** The Halls have a Map tab that shows all of long-term as a 3D
constellation (`GET /long/map`, read-only). Every core and satellite is
placed by what it means: its stored embedding, cut to three dimensions by
PCA. The lines are the structure. A satellite has a line to its first core
(home), a gold bridge with light running along it to every other core it
supports, and an older wording has a grey line to the core that took its
place (history). A core's colour comes from where it sits, so a
neighbourhood reads as one hue. **Meaning** keeps those places;
**Constellation** lets the links pull each core's episodes around it.
Clicking a memory flies the camera to it and opens its full text, its cores
and its episodes. "Light up" highlights every memory that mentions a word.
The 3D library (3d-force-graph 1.80.0, MIT) is vendored in `viewer/vendor/`
and served under `/viewer/` like the page, so the Map works on a LAN with no
internet.

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

A draft is a list of operations on long-term - `new_core` (the retelling;
the episodes it was made from land under it as its first satellites,
`kept_episode`), `attach` (a satellite on an existing core; the core's
evidence grows, its wording may be restated), `supersede` (the old core
stays, demoted, still recallable through history; the new core keeps its
episodes the same way), `verbatim`, and `retell` (the core's wording becomes
the retelling and the old wording hangs under it as its episode; no evidence
bump, no word-share guard, the reviewer is the guard, `undo_restate` still
brings the old wording back) - each reviewed on its own. The review view
flags a pending core that is its episode's own words (`core_check`) and
shows a retell beside the wording it replaces (`retell_check`):

| route                          | who        | what                                          |
|--------------------------------|------------|-----------------------------------------------|
| `POST /drafts`                 | hippocampus| submit a draft                                |
| `GET /drafts?status=pending`   | reviewer   | the queue (also the `list_drafts` MCP tool)   |
| `GET /drafts/{id}`, `/chain`   | reviewer   | one draft with every verdict; every attempt in its chain. `?review=true` (what the `get_draft` tool returns) is the reviewer's view, below |
| `POST /drafts/{id}/review`     | reviewer   | `{"decisions": [{"op": 0, "verdict": "approve"}, {"op": 1, "verdict": "deny", "critique": "..."}]}` (`review_draft`) |
| `POST /drafts/{id}/close`      | hippocampus | the cull: a reviewed draft whose chain has landed is closed; it leaves the reviewed queue (409 while pending) |
| `POST /brief/{id}/consume`     | hippocampus | the brief that opened the sleep is retired once the chain lands; `GET /brief` shows open briefs only unless `include_consumed=true` |
| `GET /long/{id}/satellites`    | anyone     | a core's surroundings                         |
| `POST /tidy`                   | hippocampus| age out, maintain near-term, sweep, and execute the flags: purge what was flagged, put back the wording of cores flagged with `undo_restate`, move satellites flagged with `link_satellite` / `unlink_satellite` |
| `POST /long/{id}/purge`        | hippocampus, or the model in an emergency | execute a purge with the cascade; `GET /tombstones` |

**Reviewing.** Each operation gets its own verdict. A denial needs a
critique, and the hippocampus redrafts from it, up to its `max_attempts`.

- **The reviewer's view.** `get_draft` puts what has to be compared in front
  of the reviewer: `target_core` (the core an `attach` or `supersede` would
  change, in full), `restate_check` on an attach that carries a rewording, and
  on a redraft `earlier_attempts` - every previous version of the operation
  with the critique that sent it back.
- **A restate cannot wipe a core.** Approving an attach with
  `restated_content` *replaces* the core's wording. A rewording that is just
  the episode's own text, or that keeps under half of the core's words, is
  refused and nothing is applied. Add `"restate": false` to the decision to
  attach the episode and leave the core's wording alone (the usual right
  answer), or `"restate": true` once you have compared the two and want it.
  On 1 Oct 2026 two cores were overwritten with their own satellites this
  way; that is why the check exists.
- **The last attempt is where things land.** A draft marked `terminal: true`
  is the last one in its chain: an operation denied there is dropped, and its
  memory stays in short-term for a later sleep. So the reviewer takes the best
  version from `earlier_attempts`, fixes what is still wrong, and approves.
  Only on a terminal draft, an approval may carry `edited_content` (the
  text), `edited_kind` and `edited_target_core_id` (the wrong kind of
  operation, or aimed at the wrong core) and `edited_restated_content`. What
  was drafted is kept on the operation as `review_edits`. Deny there only
  what should not be in memory at all.
- **An approval consumes what it names, and only that.** Approving archives
  the short-terms in `source_short_ids`. The hippocampus names a fragment only
  when the operation's text carries every sentence of it; the ones it cited
  but left out ride along as `left_short_ids`. The reviewer's view says when
  an operation would leave fragments behind (`sources_check`) and shows the
  ones still in short-term in full (`left_in_short_term`, including what an
  earlier attempt cited). On any attempt, an approval may carry
  `edited_source_short_ids`, the reviewer saying "this carries those"; it
  replaces the drafted list, and a name not in short-term is refused. Nothing
  tidies short-term against long-term: a short-term ranks above long-term in
  recall and can hold detail the core lost, so it leaves by an approval that
  names it or by aging out. Before this (19 Oct 2026), long fragments were
  left out of every attempt and each sleep drafted them again.

```json
{"decisions": [
  {"op": 0, "verdict": "approve"},
  {"op": 1, "verdict": "approve", "restate": false},
  {"op": 2, "verdict": "approve", "edited_kind": "attach", "edited_target_core_id": "<core id>"},
  {"op": 3, "verdict": "deny", "critique": "a duplicate of core <id>"},
  {"op": 4, "verdict": "approve", "edited_source_short_ids": ["<short id>", "<short id>"]}
]}
```

`GET /health` lists `features` (`target_op`, `restate_guard`, `redraft_of`,
`terminal_edits`, `edited_sources`), so a hippocampus can tell what the Memory
it talks to understands.

Long-term is a core and its surroundings: recall returns cores; satellites
(`kind: satellite`, `core_id`) and superseded cores come back only when asked
(`include_satellites`, `include_superseded`). A core's own episodes are among
its satellites (`kept_episode`), so a core can be a paragraph while the
detail still finds it (a core is as near as its nearest episode).
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
- `orbit.max_distance` (0.45) and `orbit.max_cores` (3) - how near a core a
  short has to sit to orbit it, and how many it may orbit; `orbit.enabled`
  turns the marking off

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

`/search` hits all three tiers in parallel, then merges by one score: cosine similarity plus small additive nudges (NearTerm +0.05 and ShortTerm +0.02 at equal relevance; LongTerm + ln(evidence_count) × 0.015). Relevance decides; the nudges settle ties. The shape and its reasons live in `seren_memory/ranking.py`.

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
