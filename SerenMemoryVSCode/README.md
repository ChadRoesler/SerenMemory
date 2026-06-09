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
| **Long-term** | Durable facts promoted through consolidation | Permanent until explicitly forgotten |

Long-term is a **gated tier** - nothing gets written there directly. Facts earn their way in through the consolidation cycle or an explicit promote, which means long-term stays clean and accurate over time.

---

## Requirements

- **GitHub Copilot** (Chat) - the extension registers Copilot language model tools; Copilot Chat is required to use them.
- **SerenMemory service** - the Python backend. Install it with the one-shot setup script from the [SerenMemory repository](https://github.com/ChadRoesler/SerenMemory).

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
| **Write Memory** | `#serenWrite` | Store a fact to short-term (`tier: short`) or a future intent to near-term (`tier: near`). |

### Memory lifecycle

| Tool | Reference | What it does |
|------|-----------|-------------|
| **Preserve Verbatim** | `#serenPreserveVerbatim` | Flag a short-term entry so the consolidator promotes its exact wording instead of synthesising. Use for quotes, specs, or anything where the precise words matter. |
| **Promote Now** | `#serenPromoteNow` | Immediately move a short-term entry to long-term, skipping the consolidation cycle. Use when "remember this forever" is explicit. |
| **Forget Long-Term** | `#serenForgetLong` | Flag a long-term entry for removal or correction. PII triggers a purge; other reasons demote the entry. |
| **Complete Intent** | `#serenCompleteIntent` | Mark a near-term intent as done. Completed intents are promoted to long-term as a record. |

### Consolidation

| Tool | Reference | What it does |
|------|-----------|-------------|
| **Submit Brief** | `#serenBrief` | Tell the consolidator what mattered in this session. `promote_hints` / `noise_hints` steer which topics make it to long-term. |
| **Run Consolidation** | `#serenConsolidate` | Trigger a consolidation pass immediately. |

### Draft review

The consolidator produces **drafts** before anything lands in long-term. Copilot reviews and gates them.

| Tool | Reference | What it does |
|------|-----------|-------------|
| **List Drafts** | `#serenListDrafts` | List drafts by status: `pending`, `approved`, `rejected`, `requires_selection`. |
| **Get Draft Chain** | `#serenDraftChain` | Show all synthesis attempts for one cluster - useful when a draft is in `requires_selection`. |
| **Approve Draft** | `#serenApproveDraft` | Commit a draft to long-term memory. |
| **Reject Draft** | `#serenRejectDraft` | Reject with a critique. The consolidator redrafts up to the configured limit; be specific ("conflated X with Y" beats "wrong vibe"). |
| **Select Best Draft** | `#serenSelectDraft` | When retries are exhausted and the chain is `requires_selection`, pick the best attempt (optionally with edits). |

---

## Closed-system / no-local-model mode

If you're in a locked-down environment where a local consolidator model isn't available, the service runs in **closed-system mode** automatically - just leave `model_url` blank in the config. In this mode:

- The consolidator runs its mechanical cycle (clustering, aging, promotion) normally.
- All model calls are skipped silently - no chatter, no connection attempts.
- **Copilot owns briefs, drafts, and consolidation** via the tools above. This is the intended workflow for air-gapped or security-restricted deployments.

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
