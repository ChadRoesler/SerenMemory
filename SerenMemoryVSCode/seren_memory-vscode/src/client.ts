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
 * Without this, a hung SerenMemory (DB lock, slow draft apply) means
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
  //           is written only when the main model approves a SerenHippocampus
  //           draft (reviewDraft). Use promoteNow() if you need a fact in
  //           long-term immediately (writes short, flags verbatim, promotes
  //           verbatim - same effect, ethos-respecting).

  async writeShort(content: string, topic: string, signal?: AbortSignal): Promise<unknown> {
    return this.post("/short", { content, topic }, signal);
  }

  async writeNear(intent: string, topic: string, signal?: AbortSignal): Promise<unknown> {
    return this.post("/near", { intent, topic }, signal);
  }

  /** Submit a steering brief. A brief OPENS the hippocampus's next sleep
   *  and steers its draft. The hints are TOPIC PHRASES, matched against
   *  each topic cluster's topic+content when the hippocampus decides what
   *  to draft - they're how the brief actually steers. */
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

  /** The flag means PURGE: the hippocampus executes it on its next tick,
   *  with a tombstone. Correcting a fact is a supersession in a draft,
   *  never this route. */
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

  // -- drafts -----------------------------------------------------------------
  //
  // CONTRACT NOTES (don't drift):
  //   Consolidation is SerenHippocampus's job, not this service's - the old
  //   /consolidate/* and /drafts/{id}/approve|reject|select routes are gone
  //   (404). The hippocampus submits a draft (a list of operations on
  //   long-term); the main model reviews it PER OPERATION:
  //   /drafts?status=&limit=   status is pending | reviewed | closed; omit
  //                            for every draft. Returns {count, entries}.
  //   /drafts/{id}/review      takes { decisions: [{op, verdict, critique?,
  //                            edited_content?}], note? }. verdict is
  //                            approve | deny (NOT approved/denied); a deny
  //                            needs a critique; edited_content only on a
  //                            terminal draft. 409 on re-deciding an op.

  async listDrafts(status?: string, limit?: number, signal?: AbortSignal): Promise<unknown> {
    const params = new URLSearchParams();
    if (status) params.set("status", status);
    if (limit !== undefined) params.set("limit", String(limit));
    const qs = params.toString();
    return this.get(`/drafts${qs ? `?${qs}` : ""}`, signal);
  }

  async getDraft(draftId: string, signal?: AbortSignal): Promise<unknown> {
    return this.get(`/drafts/${encodeURIComponent(draftId)}`, signal);
  }

  /** Approve or deny operations, one verdict each. Approved operations are
   *  applied by the server as it walks the list, so a bad decision halfway
   *  through can leave the earlier ones applied without the draft being
   *  saved. checkDecisions() refuses the malformed ones here, before the
   *  request goes out. */
  async reviewDraft(
    draftId: string,
    decisions: DraftDecision[],
    note?: string,
    signal?: AbortSignal
  ): Promise<unknown> {
    const body: Record<string, unknown> = { decisions: checkDecisions(decisions) };
    if (note !== undefined && note !== "") body.note = note;
    return this.post(`/drafts/${encodeURIComponent(draftId)}/review`, body, signal);
  }
}

/** One verdict on one draft operation, as POST /drafts/{id}/review takes it. */
export interface DraftDecision {
  op: number;
  verdict: "approve" | "deny";
  critique?: string;
  edited_content?: string;
}

/** Validate decisions the way the server will, and strip empty optional
 *  fields so they aren't sent. Throws on the first bad one, before any
 *  request goes out. */
export function checkDecisions(decisions: DraftDecision[]): DraftDecision[] {
  if (!Array.isArray(decisions) || decisions.length === 0) {
    throw new Error("decisions must be a non-empty list of {op, verdict, critique?}");
  }
  const seen = new Set<number>();
  return decisions.map((d) => {
    if (!Number.isInteger(d.op) || d.op < 0) {
      throw new Error(`decision op must be an operation index, got ${JSON.stringify(d.op)}`);
    }
    if (seen.has(d.op)) {
      throw new Error(`operation ${d.op} is decided twice`);
    }
    seen.add(d.op);
    const out: DraftDecision = { op: d.op, verdict: d.verdict };
    if (d.verdict === "deny") {
      if (!d.critique || d.critique.trim() === "") {
        throw new Error(`operation ${d.op}: a critique is required to deny`);
      }
      out.critique = d.critique;
    } else if (d.verdict === "approve") {
      // Blank edited_content is a 400 on the backend (stranding the earlier
      // ops, see reviewDraft); treat it as "no edit" rather than send it.
      if (d.edited_content !== undefined && d.edited_content.trim() !== "") {
        out.edited_content = d.edited_content;
      }
    } else {
      throw new Error(`operation ${d.op}: verdict must be approve or deny`);
    }
    return out;
  });
}
