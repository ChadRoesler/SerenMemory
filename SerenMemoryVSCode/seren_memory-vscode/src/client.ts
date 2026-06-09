import { SerenConfig } from "./config";

export class SerenApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly body: unknown,
    message: string
  ) {
    super(message);
    this.name = "SerenApiError";
  }
}

/**
 * HTTP client for SerenMemory. Every method's payload matches the actual
 * route contract on the SerenMemory side - names verified against the live
 * pydantic schemas. Don't drift; the backend uses pydantic v2 with the
 * default extra="ignore" config, which means unknown fields are SILENTLY
 * DROPPED. A typo'd field name doesn't 400, it just gets dropped on the
 * floor and the call appears to succeed with default behaviour.
 *
 * Every request takes an optional AbortSignal so the VS Code cancellation
 * token from a tool's invoke() can actually cancel the in-flight fetch.
 * Without this, a hung SerenMemory (DB lock, slow consolidation) means
 * tool calls hang forever even when VS Code asks them to stop.
 */
export class SerenClient {
  constructor(private readonly config: SerenConfig) {}

  // -- helpers ----------------------------------------------------------------

  private async request<T>(
    method: string,
    path: string,
    body?: unknown,
    signal?: AbortSignal
  ): Promise<T> {
    const headers = await this.config.getHeaders();
    const response = await fetch(`${this.config.endpoint}${path}`, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });

    let json: unknown;
    const ct = response.headers.get("content-type") ?? "";
    if (ct.includes("application/json")) {
      json = await response.json();
    } else {
      json = await response.text();
    }

    if (!response.ok) {
      throw new SerenApiError(
        response.status,
        json,
        `SerenMemory ${method} ${path} failed: ${response.status}`
      );
    }
    return json as T;
  }

  private get<T>(path: string, signal?: AbortSignal): Promise<T> {
    return this.request<T>("GET", path, undefined, signal);
  }

  private post<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    return this.request<T>("POST", path, body, signal);
  }

  // -- health -----------------------------------------------------------------

  async ping(): Promise<boolean> {
    try {
      await fetch(`${this.config.endpoint}/health`, { signal: AbortSignal.timeout(3000) });
      return true;
    } catch {
      return false;
    }
  }

  // -- write tiers ------------------------------------------------------------
  //
  // CONTRACT NOTES (don't drift):
  //   /short  takes ShortTermEntry  -> { content, topic? }
  //   /near   takes NearTermEntry   -> { intent, topic?, trigger_type?, ... }
  //                                    ^^^^^^ NOT `content`. Backend will 422.
  //   /long   has NO POST endpoint by design. The Lacuna boundary - long-term
  //           is written exclusively by the consolidator. Use promoteNow() if
  //           you need a fact in long-term immediately (writes short, flags
  //           verbatim, promotes verbatim - same effect, ethos-respecting).

  async writeShort(content: string, topic: string, signal?: AbortSignal): Promise<unknown> {
    return this.post("/short", { content, topic }, signal);
  }

  async writeNear(intent: string, topic: string, signal?: AbortSignal): Promise<unknown> {
    return this.post("/near", { intent, topic }, signal);
  }

  /** Submit a steering brief for the next consolidator cycle. The hints
   *  are TOPIC PHRASES, matched against entry topics+content during
   *  cluster promotion - they're how the brief actually steers. */
  async writeBrief(
    summary: string,
    promote_hints?: string[],
    noise_hints?: string[],
    completed_intents?: string[],
    signal?: AbortSignal
  ): Promise<unknown> {
    const body: Record<string, unknown> = { summary };
    if (promote_hints && promote_hints.length > 0) body.promote_hints = promote_hints;
    if (noise_hints && noise_hints.length > 0) body.noise_hints = noise_hints;
    if (completed_intents && completed_intents.length > 0) body.completed_intents = completed_intents;
    return this.post("/brief", body, signal);
  }

  // -- agency (verbatim / promote / forget / complete) ------------------------

  async preserveVerbatim(shortId: string, signal?: AbortSignal): Promise<unknown> {
    return this.post(`/short/${encodeURIComponent(shortId)}/preserve`, undefined, signal);
  }

  async promoteNow(shortId: string, signal?: AbortSignal): Promise<unknown> {
    return this.post(`/short/${encodeURIComponent(shortId)}/promote`, undefined, signal);
  }

  async forgetLong(longId: string, reason: string, signal?: AbortSignal): Promise<unknown> {
    return this.post(`/long/${encodeURIComponent(longId)}/forget`, { reason }, signal);
  }

  async completeIntent(intentId: string, signal?: AbortSignal): Promise<unknown> {
    return this.post(`/near/${encodeURIComponent(intentId)}/complete`, undefined, signal);
  }

  // -- search -----------------------------------------------------------------
  //
  // CONTRACT NOTES (don't drift):
  //   /search takes SearchRequest -> {
  //     query: string,
  //     n_results: int (NOT `limit`),
  //     include_short: bool, include_near: bool, include_long: bool
  //       (NOT a `tiers: string[]` field - that gets silently dropped),
  //     include_superseded: bool
  //   }

  async search(
    query: string,
    n_results: number = 5,
    include_short: boolean = true,
    include_near: boolean = true,
    include_long: boolean = true,
    include_superseded: boolean = false,
    signal?: AbortSignal
  ): Promise<unknown> {
    return this.post(
      "/search",
      { query, n_results, include_short, include_near, include_long, include_superseded },
      signal
    );
  }

  // -- consolidation ----------------------------------------------------------

  async wakeConsolidator(signal?: AbortSignal): Promise<unknown> {
    return this.post("/consolidate/wake", undefined, signal);
  }

  async runConsolidation(signal?: AbortSignal): Promise<unknown> {
    return this.post("/consolidate/run", undefined, signal);
  }

  // -- drafts -----------------------------------------------------------------

  async listDrafts(status?: string, signal?: AbortSignal): Promise<unknown> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return this.get(`/drafts${qs}`, signal);
  }

  async getDraftChain(draftId: string, signal?: AbortSignal): Promise<unknown> {
    return this.get(`/drafts/${encodeURIComponent(draftId)}/chain`, signal);
  }

  async approveDraft(draftId: string, note?: string, signal?: AbortSignal): Promise<unknown> {
    const body = note ? { note } : undefined;
    return this.post(`/drafts/${encodeURIComponent(draftId)}/approve`, body, signal);
  }

  async rejectDraft(draftId: string, critique: string, signal?: AbortSignal): Promise<unknown> {
    return this.post(`/drafts/${encodeURIComponent(draftId)}/reject`, { critique }, signal);
  }

  /** Commit best-of-chain to long-term. edited_content (when set) replaces
   *  the draft text; the original synthesis stays on the draft row for
   *  audit. Empty/whitespace edited_content is rejected with 400 by the
   *  backend - we send undefined to opt out rather than risking a blank. */
  async selectDraft(
    draftId: string,
    editedContent?: string,
    note?: string,
    signal?: AbortSignal
  ): Promise<unknown> {
    const body: Record<string, unknown> = {};
    if (editedContent !== undefined && editedContent.trim() !== "") {
      body.edited_content = editedContent;
    }
    if (note !== undefined && note !== "") {
      body.note = note;
    }
    const payload = Object.keys(body).length > 0 ? body : undefined;
    return this.post(`/drafts/${encodeURIComponent(draftId)}/select`, payload, signal);
  }
}
