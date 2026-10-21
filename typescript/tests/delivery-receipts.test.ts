// Modified for Norma.
import { describe, expect, it } from "vitest";
import { NormaClient, HTTPClient } from "../src/index";

function setup(receipt: unknown, status = 202, count = 2): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "receipt-agent", agentKey: "synthetic-test-key", runtimeUrl: "https://norma.invalid", httpClient: new HTTPClient({ fetcher: async (request) => {
    const result = request.url.endsWith("/checks") ? {
      execution_contract: "hosted-v1", allowed: true, confidence: 1, non_matches: Array.from({ length: count }, (_, index) => ({
        rule_id: index + 1, rule_name: "Synthetic rule", action: "observe", result: { matched: false, confidence: 1 },
      })),
    } : receipt;
    return new Response(JSON.stringify(result), { status: request.url.endsWith("/checks") ? 200 : status,
      headers: { "content-type": "application/json" } });
  } }) });
  return client;
}
const request = { stage: "pre" as const, step: { type: "tool", name: "lookup", input: {} } };

describe("truthful observation delivery receipts", () => {
  it.each([
    [{ received: 2, enqueued: 2, dropped: 0, stored: 2, duplicates: 0, status: "queued" }, "delivered", 2, 2, 0],
    [{ received: 2, enqueued: 2, dropped: 0, stored: 0, duplicates: 2, status: "queued" }, "delivered", 2, 0, 0],
    [{ received: 2, enqueued: 2, dropped: 0, stored: null, duplicates: 0, status: "queued" }, "acknowledged", 2, null, 0],
    [{ received: 2, enqueued: 2, dropped: 0, status: "queued" }, "acknowledged", 2, null, 0],
    [{ received: 2, enqueued: 1, dropped: 1, stored: 1, duplicates: 0, status: "partial" }, "failed", 1, 1, 1],
    [{ received: 2, enqueued: 2, dropped: 0, stored: 8, duplicates: 0, status: "queued" }, "unconfirmed", 2, null, 0],
  ])("reports only the server's confirmed delivery %#", async (response, status, acknowledged, persisted, dropped) => {
    // Given: each receipt represents a different persistence or queue outcome.
    const client = setup(response);
    // When: the check returns and background observation delivery is drained.
    expect((await client.checks.check(request)).allowed).toBe(true);
    const receipt = await client.flushObservability();
    // Then: a queued acknowledgement is never upgraded into confirmed persistence.
    expect(receipt).toMatchObject({ status, acknowledged, persisted, dropped, pending: 0 });
    expect(await client.flushObservability()).toEqual(receipt);
  });

  it.each([[{}, 202, "unconfirmed"], [{ received: "2", enqueued: "2", dropped: 0, stored: 2, duplicates: 0, status: "queued" }, 202, "unconfirmed"], [{ detail: "unavailable" }, 503, "failed"]])("does not report delivery on malformed or failed responses %#", async (response, status, expected) => {
    const client = setup(response, status as number);
    expect((await client.checks.check(request)).allowed).toBe(true);
    const receipt = await client.flushObservability();
    expect(receipt).toMatchObject({ status: expected, acknowledged: 0, pending: 0 });
    expect(receipt.message).not.toMatch(/not delivered|not stored/);

  });

  it("counts local queue overflow instead of claiming an empty successful flush", async () => {
    const client = setup({}, 200, 1001);
    await client.checks.check(request);
    expect(await client.flushObservability()).toMatchObject({ status: "failed", dropped: 1001, acknowledged: 0 });
  });

  it("reports an opted-out client as disabled", async () => {
    const client = new NormaClient();
    client.init({ agentName: "receipt-agent", agentKey: "synthetic-test-key", runtimeUrl: "https://norma.invalid", observabilityEnabled: false });
    expect(await client.flushObservability()).toMatchObject({ status: "disabled", acknowledged: 0, persisted: 0 });
  });
});
