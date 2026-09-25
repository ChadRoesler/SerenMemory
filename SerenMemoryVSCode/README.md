# Seren Memory

Persistent, tiered memory for GitHub Copilot. Seren Memory connects Copilot to a locally-hosted memory service so facts, preferences, and project context survive across sessions - without sending anything to a third party.

---

## How it works

Seren Memory runs a small Python service on your machine (or your team's internal server). The extension registers a set of Copilot language model tools that Copilot calls automatically to read and write memory during normal conversations. You stay in full control: everything lives in a local [ChromaDB](https://www.trychroma.com/) database that you own.

### Three memory tiers

| Tier | What goes here | Lifetime |
|------|---------------|---------|
| **Short-term** | Session observations, notes, working context | Days - ages out automatically |
| **Near-term** | Future intents: "remind me to X", "do Y next time" | Until completed or expired |
| **Long-term** | Durable facts from hippocampus drafts you approved | Permanent until flagged for purge |

Long-term is a **gated tier** - nothing gets written there directly. Consolidation is done by a separate service, **SerenHippocampus**: while it sleeps it clusters short-term entries by topic and submits a **draft** of operations on long-term. A fact lands in long-term only when the main model (Copilot) approves that operation, or through an explicit promote - which means long-term stays clean and accurate over time.

---

## Requirements

- **GitHub Copilot** (Chat) - the extension registers Copilot language model tools; Copilot Chat is required to use them.
- **SerenMemory service** - the Python backend. Install it with the one-shot setup script from the [SerenMemory repository](https://github.com/ChadRoesler/SerenMemory).
- **SerenHippocampus** - the consolidation service that writes drafts. Without it, short-term and near-term still work, but nothing new reaches long-term except by **Promote Now**.

---

## Quick start

**1. Install the SerenMemory service**

Windows:
```powershell
.\seren-memory-setup.ps1 -Mcp -GenToken -AutoStart
```

macOS / Linux:
```bash
bash seren-memory-setup.sh --mcp --gen-token --service
```

Both scripts print the exact MCP config JSON to paste into your editor at the end.

**2. Configure the extension**

Open **Settings** (`Ctrl+Shift+P` → `Open User Settings`) and set:

| Setting | Default | Description |
|---------|---------|-------------|
| `serenMemory.endpoint` | `http://localhost:7420` | Base URL of the SerenMemory service |
| `serenMemory.startCommand` | `python -m seren_memory` | Command used by **Start Service** |
| `serenMemory.suppressStartPrompt` | `false` | Suppress the startup "not reachable" prompt |

**3. Set your bearer token (if auth is enabled)**

Run `Ctrl+Shift+P` → **Seren Memory: Set Bearer Token**. The token is stored in the OS keychain, never in settings files.

**4. Check the status bar**

The `$(database) Seren` item in the bottom-right of the status bar shows service health at a glance:

- `Seren ✓` - service is reachable
- `Seren ✗` - service is not reachable (click to retry)

---

## Copilot tools

Once the service is running, Copilot can use these tools automatically. You can also reference them directly in chat with `#serenSearch`, `#serenWrite`, etc.

### Memory access

| Tool | Reference | What it does |
|------|-----------|-------------|
| **Search Memory** | `#serenSearch` | Retrieve relevant past context before answering. The main recall path - call before anything that might benefit from prior knowledge. |
| **Write Memory** | `#serenWrite` | Store a fact to short-term (`tier: short`) or a future intent to near-term (`tier: near`). The `topic` drives the hippocampus's clustering, so reuse it for related entries. |

### Memory lifecycle

| Tool | Reference | What it does |
|------|-----------|-------------|
| **Preserve Verbatim** | `#serenPreserveVerbatim` | Flag a short-term entry so the hippocampus drafts it as a verbatim operation, exact wording kept, instead of synthesising. Use for quotes, specs, or anything where the precise words matter. |
| **Promote Now** | `#serenPromoteNow` | Immediately move a short-term entry to long-term, skipping the hippocampus and draft review. Use when "remember this forever" is explicit. |
| **Forget Long-Term** | `#serenForgetLong` | Flag a long-term entry for purging. The hippocampus executes the purge on its next tick and leaves a tombstone (id and reason, never the content). Not for corrections - a corrected fact is a new memory that a draft supersedes the old one with. |
| **Complete Intent** | `#serenCompleteIntent` | Mark a near-term intent as done. At the next sleep it leaves the open loops and becomes a long-term record. |

### The sleep

| Tool | Reference | What it does |
|------|-----------|-------------|
| **Submit Brief** | `#serenBrief` | Tell the hippocampus what mattered in this session. A brief opens its next sleep and steers the draft; `promote_hints` / `noise_hints` decide which topic clusters get drafted. |

### Draft review

After a sleep the hippocampus submits a **draft**: a list of operations on long-term, each reviewed on its own.

- `new_core` - a durable statement with the lesson in it
- `attach` - new evidence for an existing core (the core's evidence grows)
- `supersede` - a new core that overrides an old one; the old one stays, demoted, as history
- `verbatim` - one entry kept word for word as its own core

| Tool | Reference | What it does |
|------|-----------|-------------|
| **List Drafts** | `#serenListDrafts` | The review queue. Filter by status: `pending` (default), `reviewed`, `closed`, or `all`. |
| **Get Draft** | `#serenGetDraft` | One draft with every operation, its rationale, and its verdict so far. Read it before reviewing. |
| **Review Draft** | `#serenReviewDraft` | Approve or deny each operation. Approving applies it to long-term now. Denying needs a critique; the hippocampus redrafts that operation and resubmits it as the next attempt, so be specific ("conflates X with Y; separate them" beats "wrong vibe"). On the last permitted attempt (`terminal: true`) an approval may carry `edited_content`. |

---

## Closed-system / no-local-model mode

If you're in a locked-down environment where a local model isn't available, run SerenHippocampus without one. In this mode:

- The hippocampus sleeps mechanically: topic clusters over the threshold become `new_core` operations from their longest entry, and verbatim flags are honoured. It never proposes `attach` or `supersede` - those are judgements, and a threshold is not one.
- **Copilot still owns briefs and the review** via the tools above. This is the intended workflow for air-gapped or security-restricted deployments.

---

## Commands

Open the Command Palette (`Ctrl+Shift+P`) and search **Seren Memory**:

| Command | What it does |
|---------|-------------|
| **Seren Memory: Set Bearer Token** | Store your auth token in the OS keychain |
| **Seren Memory: Check Service Health** | Ping the service and update the status bar |
| **Seren Memory: Start Service** | Launch the service using `serenMemory.startCommand` |

---

## MCP transport (optional)

The service also exposes an MCP HTTP endpoint at `/mcp/`. This lets you connect directly via the VS Code or Visual Studio MCP client config without the extension, or use both at the same time.

Install with the `--mcp` flag and paste the config the setup script prints:

**`.vscode/mcp.json`** (VS Code):
```json
{
  "servers": {
	"seren-memory": {
	  "type": "http",
	  "url": "http://localhost:7420/mcp/",
	  "headers": {
		"Authorization": "Bearer YOUR_TOKEN"
	  }
	}
  }
}
```

**`.vs/mcp.json`** (Visual Studio):
```json
{
  "servers": {
	"seren-memory": {
	  "type": "http",
	  "url": "http://localhost:7420/mcp/",
	  "headers": {
		"Authorization": "Bearer YOUR_TOKEN"
	  }
	}
  }
}
```

Omit the `headers` block if you didn't set a bearer token.

---

## Memory viewer

The service ships a browser UI at `http://localhost:7420/viewer` - short-term, near-term, long-term, briefs, drafts, and search, all in one place. If auth is enabled it prompts for the bearer token on load.

---

## Source & issues

[github.com/ChadRoesler/SerenMemory](https://github.com/ChadRoesler/SerenMemory)
