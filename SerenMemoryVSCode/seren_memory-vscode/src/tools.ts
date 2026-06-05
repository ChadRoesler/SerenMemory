import * as vscode from "vscode";
import { SerenClient, SerenApiError } from "./client";

// ── helpers ────────────────────────────────────────────────────────────────

function ok(text: string): vscode.LanguageModelToolResult {
  return new vscode.LanguageModelToolResult([
    new vscode.LanguageModelTextPart(text),
  ]);
}

function err(e: unknown): vscode.LanguageModelToolResult {
  if (e instanceof SerenApiError) {
    return ok(`Error ${e.status}: ${JSON.stringify(e.body)}`);
  }
  if (e instanceof Error && e.name === "AbortError") {
    return ok("Cancelled by user/host.");
  }
  return ok(`Error: ${String(e)}`);
}

function json(data: unknown): vscode.LanguageModelToolResult {
  return ok(JSON.stringify(data, null, 2));
}

/** Bridge VS Code's CancellationToken to an AbortSignal so the underlying
 *  fetch can actually cancel. Without this, a hung SerenMemory means the
 *  tool call hangs forever regardless of what VS Code's cancel button does.
 *
 *  The returned controller's signal should be threaded into every client
 *  call inside the invoke. We don't need to detach the listener because
 *  the controller is GC'd with the tool invocation. */
function signalFromToken(token: vscode.CancellationToken): AbortSignal {
  const controller = new AbortController();
  if (token.isCancellationRequested) {
    controller.abort();
  } else {
    token.onCancellationRequested(() => controller.abort());
  }
  return controller.signal;
}

// ── seren_memory_search ────────────────────────────────────────────────────

interface SearchInput {
  query: string;
  n_results?: number;
  include_short?: boolean;
  include_near?: boolean;
  include_long?: boolean;
  include_superseded?: boolean;
}

export class SearchTool implements vscode.LanguageModelTool<SearchInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<SearchInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    const {
      query,
      n_results = 5,
      include_short = true,
      include_near = true,
      include_long = true,
      include_superseded = false,
    } = options.input;
    try {
      const result = await this.client.search(
        query, n_results, include_short, include_near, include_long, include_superseded,
        signalFromToken(token)
      );
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_write ─────────────────────────────────────────────────────

interface WriteInput {
  content: string;
  topic: string;
  tier?: "short" | "near";
}

export class WriteTool implements vscode.LanguageModelTool<WriteInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<WriteInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    const { content, topic, tier = "short" } = options.input;
    const signal = signalFromToken(token);
    try {
      let result: unknown;
      if (tier === "near") {
        // Near-term takes `intent`, not `content` - the field name in the
        // pydantic schema. The plugin maps `content` from the tool input
        // to the backend's `intent` here so the model sees one consistent
        // input parameter regardless of tier.
        result = await this.client.writeNear(content, topic, signal);
      } else {
        result = await this.client.writeShort(content, topic, signal);
      }
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_brief ─────────────────────────────────────────────────────

interface BriefInput {
  summary: string;
  promote_hints?: string[];
  noise_hints?: string[];
  completed_intents?: string[];
}

export class BriefTool implements vscode.LanguageModelTool<BriefInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<BriefInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    const { summary, promote_hints, noise_hints, completed_intents } = options.input;
    try {
      const result = await this.client.writeBrief(
        summary, promote_hints, noise_hints, completed_intents,
        signalFromToken(token)
      );
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_consolidate ───────────────────────────────────────────────

export class ConsolidateTool implements vscode.LanguageModelTool<object> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    _options: vscode.LanguageModelToolInvocationOptions<object>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    const signal = signalFromToken(token);
    try {
      await this.client.wakeConsolidator(signal);
      const result = await this.client.runConsolidation(signal);
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_preserve_verbatim ─────────────────────────────────────────

interface ShortIdInput {
  short_id: string;
}

export class PreserveVerbatimTool implements vscode.LanguageModelTool<ShortIdInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ShortIdInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.preserveVerbatim(
        options.input.short_id, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_promote_now ───────────────────────────────────────────────

export class PromoteNowTool implements vscode.LanguageModelTool<ShortIdInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ShortIdInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.promoteNow(
        options.input.short_id, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_forget_long ───────────────────────────────────────────────

interface ForgetLongInput {
  long_id: string;
  reason: string;
}

export class ForgetLongTool implements vscode.LanguageModelTool<ForgetLongInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ForgetLongInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.forgetLong(
        options.input.long_id, options.input.reason, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_complete_intent ───────────────────────────────────────────

interface IntentIdInput {
  intent_id: string;
}

export class CompleteIntentTool implements vscode.LanguageModelTool<IntentIdInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<IntentIdInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.completeIntent(
        options.input.intent_id, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_list_drafts ───────────────────────────────────────────────

interface ListDraftsInput {
  status?: string;
}

export class ListDraftsTool implements vscode.LanguageModelTool<ListDraftsInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ListDraftsInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.listDrafts(
        options.input.status, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_draft_chain ───────────────────────────────────────────────

interface DraftIdInput {
  draft_id: string;
}

export class DraftChainTool implements vscode.LanguageModelTool<DraftIdInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<DraftIdInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.getDraftChain(
        options.input.draft_id, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_approve_draft ─────────────────────────────────────────────

interface ApproveInput {
  draft_id: string;
  note?: string;
}

export class ApproveDraftTool implements vscode.LanguageModelTool<ApproveInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<ApproveInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.approveDraft(
        options.input.draft_id, options.input.note, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_reject_draft ──────────────────────────────────────────────

interface RejectInput {
  draft_id: string;
  critique: string;
}

export class RejectDraftTool implements vscode.LanguageModelTool<RejectInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<RejectInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    try {
      const result = await this.client.rejectDraft(
        options.input.draft_id, options.input.critique, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}

// ── seren_memory_select_draft ──────────────────────────────────────────────

interface SelectInput {
  draft_id: string;
  edited_content?: string;
  note?: string;
}

export class SelectDraftTool implements vscode.LanguageModelTool<SelectInput> {
  constructor(private readonly client: SerenClient) {}

  async invoke(
    options: vscode.LanguageModelToolInvocationOptions<SelectInput>,
    token: vscode.CancellationToken
  ): Promise<vscode.LanguageModelToolResult> {
    const { draft_id, edited_content, note } = options.input;
    try {
      const result = await this.client.selectDraft(
        draft_id, edited_content, note, signalFromToken(token));
      return json(result);
    } catch (e) {
      return err(e);
    }
  }
}
