// Modified for Norma.
import { checkFor } from "../src/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { NormaClient } from "../src/client";
import { checkpoint } from "../src/checkpoint";
import { RuleViolationError } from "../src/errors";

const request = { agentName: "demo-agent", stage: "pre" as const, step: { type: "tool", name: "write", input: { value: "private proposal" } } };
const match = (id: number, extra = {}) => ({
  rule_id: id, rule_name: `rule-${id}`, action: "deny",
  result: { confidence: 0.7, matched: false, message: "reason", ...extra },
});
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), {
  status, headers: { "Content-Type": "application/json" },
});
function client(enabled = true): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "demo-agent", agentKey: "synthetic-test-key", runtimeUrl: "http://localhost:8000", observabilityEnabled: enabled });
  return client;
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("observed check reporting", () => {
  it("reports all observed categories and sanitized condition context with exact supplied IDs", async () => {
    // Given: matched, unmatched, unresolved and evaluator-error results from one check.
    const fetch = vi.fn().mockResolvedValueOnce(json({
      execution_contract: "hosted-v1", allowed: false, confidence: 0.7,
      matches: [{ ...match(1, { matched: true, outcome: "matched" }), check_id: "engine-id" }],
      non_matches: [match(2)], errors: [match(3, { error: "provider unavailable" })],
      unresolved: [{ ...match(4, { outcome: "unresolved", unresolved_evidence_ids: ["source"], metadata: {
        condition_trace: [{ semantic_observation: { observation_id: "obs", evidence: { value: "do not export" }, status: "insufficient_evidence" }, selected_data: "secret" }],
      } }), effective_action: "observe" }],
    })).mockResolvedValueOnce(json({ received: 4, enqueued: 4, dropped: 0, status: "queued" }, 202));
    vi.stubGlobal("fetch", fetch);
    // When: a public check returns.
    const sdk = client();
    const result = await checkFor(sdk).check(request);
    await sdk.flushObservability();
    // Then: observations retain result semantics without adding retained request evidence.
    expect(result.allowed).toBe(false);
    const body = JSON.parse(await (fetch.mock.calls.map(([req]) => req as Request).filter((req) => req.url.endsWith("/observability/events"))[0]!).text());
    expect(body.events).toHaveLength(4);
    expect(body.events[0]).toMatchObject({ check_id: "engine-id", action: "deny", matched: true, metadata: { outcome: "matched", configured_action: "deny", execution_id_source: "engine" } });
    expect(body.events.find((event: { rule_id: number }) => event.rule_id === 3).error_message).toBe("provider unavailable");
    expect(body.events[3]).toMatchObject({ action: "observe", matched: false, metadata: { outcome: "unresolved", unresolved_evidence_ids: ["source"] } });
    expect(JSON.stringify(body)).not.toMatch(/secret|do not export|private proposal/);
  });

  it("keeps legacy observations distinct even when correlation IDs repeat", async () => {
    // Given: an older server omits execution IDs on repeated identical decisions.
    const fetch = vi.fn().mockImplementation((req: Request) => Promise.resolve(req.url.endsWith("/checks")
      ? json({ execution_contract: "hosted-v1", allowed: true, confidence: 1, non_matches: [match(1)] })
      : json({ received: 1, enqueued: 1, dropped: 0, status: "queued" }, 202)));
    vi.stubGlobal("fetch", fetch);
    const sdk = client();
    // When: two real checks occur.
    await checkFor(sdk).check(request);
    await checkFor(sdk).check(request);
    await sdk.flushObservability();
    // Then: reused absent correlation context cannot collapse separate observations.
    const first = JSON.parse(await (fetch.mock.calls.map(([req]) => req as Request).filter((req) => req.url.endsWith("/observability/events"))[0]!).text()).events[0];
    const second = JSON.parse(await (fetch.mock.calls.map(([req]) => req as Request).filter((req) => req.url.endsWith("/observability/events"))[1]!).text()).events[0];
    expect(first.trace_id).toBe(second.trace_id);
    expect(first.check_id).not.toBe(second.check_id);
    expect(first.metadata.execution_id_source).toBe("sdk_occurrence");
  });

  it.each([false, true])("keeps safe/deny enforcement when reporting fails (deny=%s)", async (denied) => {
    // Given: ingestion fails after a valid safety decision.
    vi.stubGlobal("fetch", vi.fn().mockImplementation((req: Request) => Promise.resolve(req.url.endsWith("/checks")
      ? json({ execution_contract: "hosted-v1", allowed: !denied, confidence: 1, [denied ? "matches" : "non_matches"]: [match(1, { matched: denied })] })
      : json({ message: "report unavailable" }, 503))));
    const execute = vi.fn(async () => "ran");
    // When: a public decorated function runs.
    const result = checkpoint(execute, { client: client() })();
    // Then: only the rule decision determines whether the function runs.
    if (denied) {
      await expect(result).rejects.toBeInstanceOf(RuleViolationError);
      expect(execute).not.toHaveBeenCalled();
    } else {
      await expect(result).resolves.toBe("ran");
      expect(execute).toHaveBeenCalledOnce();
    }
  });

  it("returns a safety decision before a slow reporter finishes and exposes an explicit drain", async () => {
    // Given: check responds immediately while observation ingestion is pending.
    let finishReport!: (response: Response) => void;
    let reportStarted!: () => void;
    const started = new Promise<void>((resolve) => { reportStarted = resolve; });
    vi.stubGlobal("fetch", vi.fn().mockImplementation((req: Request) => req.url.endsWith("/checks")
      ? Promise.resolve(json({ execution_contract: "hosted-v1", allowed: true, confidence: 1, non_matches: [match(1)] }))
      : new Promise<Response>((resolve) => { finishReport = resolve; reportStarted(); })));
    const sdk = client();
    // When: check returns even though the report has not resolved.
    const result = await checkFor(sdk).check(request);
    let drained = false;
    const drain = sdk.flushObservability().then(() => { drained = true; });
    // Then: enforcement has its result; shutdown can explicitly wait for delivery.
    expect(result.allowed).toBe(true);
    expect(drained).toBe(false);
    await started;
    finishReport(json({ received: 1, enqueued: 1, dropped: 0, status: "queued" }, 202));
    await drain;
    expect(drained).toBe(true);
  });

  it("allows reporting opt-out without disabling check", async () => {
    // Given: reporting is disabled explicitly.
    const fetch = vi.fn().mockResolvedValue(json({ execution_contract: "hosted-v1", allowed: true, confidence: 1, non_matches: [match(1)] }));
    vi.stubGlobal("fetch", fetch);
    // When: evaluating normally.
    await checkFor(client(false)).check(request);
    // Then: no ingestion request is made.
    expect(fetch).toHaveBeenCalledOnce();
  });
});
