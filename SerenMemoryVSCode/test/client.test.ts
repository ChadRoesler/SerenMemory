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
import { SerenClient, SerenApiError, checkDecisions } from "../seren_memory-vscode/src/client";
import { SerenConfig } from "../seren_memory-vscode/src/config";
import { SecretStorage } from "./mocks/vscode";

// -- helpers ------------------------------------------------------------------

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

// -- ping ---------------------------------------------------------------------

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

// -- writeShort ---------------------------------------------------------------

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

// -- writeNear ----------------------------------------------------------------

describe("writeNear", () => {
  it("POSTs to /near with intent and topic (not content)", async () => {
    mockFetch(200, { ok: true, id: "xyz" });
    await makeClient().writeNear("follow up on PR", "dev");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/near");
    expect(JSON.parse(init.body as string)).toEqual({ intent: "follow up on PR", topic: "dev" });
  });
});

// -- search -------------------------------------------------------------------

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

// -- listDrafts ---------------------------------------------------------------

describe("listDrafts", () => {
  it("GETs /drafts with status and limit", async () => {
    mockFetch(200, { count: 0, entries: [] });
    const result = await makeClient().listDrafts("pending", 5);
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts?status=pending&limit=5");
    expect(init.method).toBe("GET");
    expect(init.body).toBeUndefined();
    expect(result).toEqual({ count: 0, entries: [] });
  });

  it("sends no query string when status and limit are omitted", async () => {
    mockFetch(200, { count: 0, entries: [] });
    await makeClient().listDrafts();
    const [url] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts");
  });
});

// -- getDraft -----------------------------------------------------------------

describe("getDraft", () => {
  it("GETs /drafts/:id", async () => {
    mockFetch(200, { id: "draft-1", operations: [], terminal: false });
    const result = await makeClient().getDraft("draft-1");
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts/draft-1");
    expect(init.method).toBe("GET");
    expect(result).toEqual({ id: "draft-1", operations: [], terminal: false });
  });
});

// -- reviewDraft --------------------------------------------------------------

describe("reviewDraft", () => {
  it("POSTs to /drafts/:id/review with decisions and note", async () => {
    mockFetch(200, { ok: true, status: "reviewed" });
    await makeClient().reviewDraft(
      "draft-2",
      [
        { op: 0, verdict: "approve" },
        { op: 1, verdict: "deny", critique: "conflates X with Y" },
      ],
      "looks mostly right"
    );
    const [url, init] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts/draft-2/review");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      decisions: [
        { op: 0, verdict: "approve" },
        { op: 1, verdict: "deny", critique: "conflates X with Y" },
      ],
      note: "looks mostly right",
    });
  });

  it("omits note when not provided", async () => {
    mockFetch(200, { ok: true });
    await makeClient().reviewDraft("draft-2", [{ op: 0, verdict: "approve" }]);
    const [, init] = lastFetch();
    expect(JSON.parse(init.body as string)).toEqual({ decisions: [{ op: 0, verdict: "approve" }] });
  });

  it("includes edited_content on an approve when non-empty", async () => {
    mockFetch(200, { ok: true });
    await makeClient().reviewDraft("draft-3", [
      { op: 0, verdict: "approve", edited_content: "revised text" },
    ]);
    const [, init] = lastFetch();
    expect(JSON.parse(init.body as string)).toEqual({
      decisions: [{ op: 0, verdict: "approve", edited_content: "revised text" }],
    });
  });

  it("drops whitespace-only edited_content", async () => {
    mockFetch(200, { ok: true });
    await makeClient().reviewDraft("draft-3", [
      { op: 0, verdict: "approve", edited_content: "   " },
    ]);
    const [, init] = lastFetch();
    expect(JSON.parse(init.body as string)).toEqual({
      decisions: [{ op: 0, verdict: "approve" }],
    });
  });

  it("refuses a deny without a critique before sending anything", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(
      makeClient().reviewDraft("draft-4", [
        { op: 0, verdict: "approve" },
        { op: 1, verdict: "deny", critique: "  " },
      ])
    ).rejects.toThrow(/critique is required/);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });

  it("refuses an empty decisions list", async () => {
    vi.stubGlobal("fetch", vi.fn());
    await expect(makeClient().reviewDraft("draft-4", [])).rejects.toThrow(/non-empty/);
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});

// -- checkDecisions -----------------------------------------------------------

describe("checkDecisions", () => {
  it("rejects an unknown verdict", () => {
    expect(() => checkDecisions([{ op: 0, verdict: "approved" as any }])).toThrow(/approve or deny/);
  });

  it("rejects a non-integer op", () => {
    expect(() => checkDecisions([{ op: 1.5, verdict: "approve" }])).toThrow(/operation index/);
  });

  it("rejects the same op decided twice", () => {
    expect(() =>
      checkDecisions([
        { op: 0, verdict: "approve" },
        { op: 0, verdict: "deny", critique: "no" },
      ])
    ).toThrow(/decided twice/);
  });

  it("drops a critique on an approve", () => {
    expect(checkDecisions([{ op: 2, verdict: "approve", critique: "fine" }])).toEqual([
      { op: 2, verdict: "approve" },
    ]);
  });
});

// -- SerenApiError -------------------------------------------------------------

describe("SerenApiError", () => {
  it("is thrown on non-2xx responses", async () => {
    mockFetch(404, { detail: "no draft 'nope'" });
    await expect(makeClient().getDraft("nope")).rejects.toBeInstanceOf(SerenApiError);
  });

  it("carries status and body", async () => {
    expect.assertions(3);
    mockFetch(409, { detail: "operation 0 is already approved" });
    try {
      await makeClient().reviewDraft("done", [{ op: 0, verdict: "approve" }]);
    } catch (e) {
      expect(e).toBeInstanceOf(SerenApiError);
      expect((e as SerenApiError).status).toBe(409);
      expect((e as SerenApiError).body).toEqual({ detail: "operation 0 is already approved" });
    }
  });
});

// -- URL encoding --------------------------------------------------------------

describe("URL encoding", () => {
  it("encodes special characters in IDs", async () => {
    mockFetch(200, { ok: true });
    await makeClient().forgetLong("id/with/slashes", "test");
    const [url] = lastFetch();
    expect(url).toBe("http://localhost:7420/long/id%2Fwith%2Fslashes/forget");
  });

  it("encodes special characters in draft IDs", async () => {
    mockFetch(200, { ok: true });
    await makeClient().reviewDraft("id/with/slashes", [{ op: 0, verdict: "approve" }]);
    const [url] = lastFetch();
    expect(url).toBe("http://localhost:7420/drafts/id%2Fwith%2Fslashes/review");
  });
});
