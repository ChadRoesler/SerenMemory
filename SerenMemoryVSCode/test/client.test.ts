/**
 * Unit tests for SerenClient.
 *
 * Uses vitest + fetch mocking (no VS Code host, no live service).
 * The `vscode` module is aliased to test/mocks/vscode.ts in vitest.config.ts
 * so the import chain (client -> config -> vscode) resolves cleanly.
 *
 * Pattern: for each method, stub globalThis.fetch to return a canned response,
 * call the method, assert the right URL/method/body was sent and the return
 * value is passed through.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { SerenClient, SerenApiError } from "../seren_memory-vscode/src/client";
import { SerenConfig } from "../seren_memory-vscode/src/config";
import { SecretStorage } from "./mocks/vscode";

// ── helpers ──────────────────────────────────────────────────────────────────

function makeClient(endpoint = "http://localhost:7420"): SerenClient {
  // SerenConfig reads endpoint from vscode.workspace.getConfiguration which
  // returns the default value from our stub - override by pointing the stub
  // at the right value via a custom getter below.
  const secrets = new SecretStorage();
  const config = new SerenConfig(secrets as any);
  // Patch endpoint getter for tests that need a specific URL.
  Object.defineProperty(config, "endpoint", { get: () => endpoint });
  return new SerenClient(config);
}

function mockFetch(status: number, body: unknown): void {
  const response = new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response));
}

function lastFetch() {
  return (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => vi.restoreAllMocks());

// ── ping ─────────────────────────────────────────────────────────────────────

describe("ping", () => {
  it("returns true when /health responds", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("{}", { status: 200 })));
    expect(await makeClient().ping()).toBe(true);
  });

  it("returns false when fetch throws (service down)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("ECONNREFUSED")));
    expect(await makeClient().ping()).toBe(false);
  });
});

// ── writeShort ───────────────────────────────────────────────────────────────

describe("writeShort", () => {
  it("POSTs to /short with content and topic", async () => {
    mockFetch(200, { ok: true, id: "abc" });
    const result = await makeClient().writeShort("test content", "testing");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/short");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ content: "test content", topic: "testing" });
    expect(result).toEqual({ ok: true, id: "abc" });
  });
});

// ── writeNear ────────────────────────────────────────────────────────────────

describe("writeNear", () => {
  it("POSTs to /near with intent and topic (not content)", async () => {
    mockFetch(200, { ok: true, id: "xyz" });
    await makeClient().writeNear("follow up on PR", "dev");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/near");
    expect(JSON.parse(init.body as string)).toEqual({ intent: "follow up on PR", topic: "dev" });
  });
});

// ── search ───────────────────────────────────────────────────────────────────

describe("search", () => {
  it("POSTs to /search with all params", async () => {
    mockFetch(200, { hits: [] });
    await makeClient().search("hardware", 3, true, false, true, false);
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/search");
    expect(JSON.parse(init.body as string)).toEqual({
      query: "hardware",
      n_results: 3,
      include_short: true,
      include_near: false,
      include_long: true,
      include_superseded: false,
    });
  });
});

// ── approveDraft ─────────────────────────────────────────────────────────────

describe("approveDraft", () => {
  it("POSTs to /drafts/:id/approve with no body when note omitted", async () => {
    mockFetch(200, { ok: true });
    await makeClient().approveDraft("draft-1");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts/draft-1/approve");
    expect(init.body).toBeUndefined();
  });

  it("includes note in body when provided", async () => {
    mockFetch(200, { ok: true });
    await makeClient().approveDraft("draft-1", "looks good");
    const [, init] = lastFetch();
    expect(JSON.parse(init.body as string)).toEqual({ note: "looks good" });
  });
});

// ── rejectDraft ───────────────────────────────────────────────────────────────

describe("rejectDraft", () => {
  it("POSTs to /drafts/:id/reject with critique", async () => {
    mockFetch(200, { ok: true, action: "redrafted" });
    await makeClient().rejectDraft("draft-2", "conflated X with Y");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts/draft-2/reject");
    expect(JSON.parse(init.body as string)).toEqual({ critique: "conflated X with Y" });
  });
});

// ── selectDraft ───────────────────────────────────────────────────────────────

describe("selectDraft", () => {
  it("sends no body when neither edited_content nor note provided", async () => {
    mockFetch(200, { ok: true });
    await makeClient().selectDraft("draft-3");
    const [, init] = lastFetch();
    expect(init.body).toBeUndefined();
  });

  it("omits edited_content when whitespace-only", async () => {
    mockFetch(200, { ok: true });
    await makeClient().selectDraft("draft-3", "   ");
    const [, init] = lastFetch();
    expect(init.body).toBeUndefined();
  });

  it("includes edited_content when non-empty", async () => {
    mockFetch(200, { ok: true });
    await makeClient().selectDraft("draft-3", "revised text", "my note");
    const [, init] = lastFetch();
    expect(JSON.parse(init.body as string)).toEqual({
      edited_content: "revised text",
      note: "my note",
    });
  });
});

// ── SerenApiError ─────────────────────────────────────────────────────────────

describe("SerenApiError", () => {
  it("is thrown on non-2xx responses", async () => {
    mockFetch(404, { detail: "not found" });
    await expect(makeClient().approveDraft("nope")).rejects.toBeInstanceOf(SerenApiError);
  });

  it("carries status and body", async () => {
    mockFetch(409, { detail: "already reviewed" });
    try {
      await makeClient().approveDraft("done");
    } catch (e) {
      expect(e).toBeInstanceOf(SerenApiError);
      expect((e as SerenApiError).status).toBe(409);
      expect((e as SerenApiError).body).toEqual({ detail: "already reviewed" });
    }
  });
});

// ── URL encoding ──────────────────────────────────────────────────────────────

describe("URL encoding", () => {
  it("encodes special characters in IDs", async () => {
    mockFetch(200, { ok: true });
    await makeClient().forgetLong("id/with/slashes", "test");
    const [url] = lastFetch();
    expect(url).toBe("http://localhost:7420/long/id%2Fwith%2Fslashes/forget");
  });
});
