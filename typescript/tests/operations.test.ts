// Modified for Norma.
import { describe, expect, it, vi } from "vitest";
import { NormaClient, HTTPClient, NormaAPIError } from "../src/index";
import type { CreateOperation } from "../src/index";

const AGENT = "operations-agent";
const operationId = "f84a7edc-0d5f-4b6c-b31c-bd5cd5adbe99";
const proposal: CreateOperation = {
  operationKey: "proposal-1", step: { type: "tool", name: "write_record", input: { price_usd: 450 } },
};
const wireRequest = {
  execution_mode: "hosted", agent_name: AGENT, operation_key: "proposal-1", stage: "pre", error_policy: "block", unresolved_policy: "block",
  step: { type: "tool", name: "write_record", input: { price_usd: 450 } }, evidence: [],
};
function receipt(request: unknown = wireRequest) {
  return {
    id: operationId, operation_key: "proposal-1", agent_name: AGENT,
    target_type: null, target_id: null, created_at: "2026-10-09T19:00:00Z", request_fingerprint: "request-fingerprint",
    request, decision: { outcome: "allow", reason: "Server rules passed", check: {
      execution_contract: "hosted-v1", allowed: true, confidence: 1, matches: [], non_matches: [], errors: [], unresolved: [], recovery_feedback: [],
    } },
    rules: [], observations: [], verifications: [], discrepancy: "none", current_outcome: "not_observed",
  };
}
function json(body: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });
}
function harness(respond: (request: Request) => Promise<Response>, config: {
  agentKey?: string | (() => Promise<string>); targetType?: string; targetId?: string;
  controlContract?: "rule-controls-v1";
} = {}) {
  const requests: Request[] = [];
  const transport = new HTTPClient({ fetcher: async (input) => {
    if (!(input instanceof Request)) throw new Error("Expected SDK Request");
    requests.push(input.clone());
    return respond(input);
  } });
  const client = new NormaClient();
  client.init({ agentName: AGENT, runtimeUrl: "https://runtime.invalid/base", agentKey: "test-key", httpClient: transport, ...config });
  return { client, requests };
}

describe("bound operation facade over the SDK HTTP transport", () => {
  it("uses the configured transport, dynamic credential and blocking defaults", async () => {
    // Given: a transport returning a synthetic server receipt for the submitted proposal.
    const agentKey = vi.fn(async () => "resolved-test-key");
    const { client, requests } = harness(async (request) => json(receipt(await request.json())), { agentKey });
    // When: retaining a proposed operation through the public client.
    const result = await client.operations.create(proposal);
    // Then: no effect is claimed, the exact proposal and public transport are retained.
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe("https://runtime.invalid/base/api/v1/operations");
    expect(requests[0].headers.get("X-API-Key")).toBe("resolved-test-key");
    expect(await requests[0].json()).toEqual(wireRequest);
    expect(agentKey).toHaveBeenCalledTimes(1);
    expect(result.decision.outcome).toBe("allow");
    expect(result.currentOutcome).toBe("not_observed");
    expect(result.observations).toEqual([]);
    expect(result.verifications).toEqual([]);
    expect(result.request.step.input).toEqual({ price_usd: 450 });
  });

  it("snapshots nested proposal and evidence before waiting for credentials", async () => {
    // Given: authentication is pending while the caller still owns mutable data.
    let resolveKey!: (key: string) => void;
    const key = new Promise<string>((resolve) => { resolveKey = resolve; });
    const { client, requests } = harness(async (request) => json(receipt(await request.json())), { agentKey: () => key });
    const request: CreateOperation = { ...proposal, step: { ...proposal.step, input: { amount: 450 } },
      evidence: [{ id: "source-1", summary: "Original proposal", source: "caller" }] };
    // When: the caller mutates its copy after starting the request.
    const pending = client.operations.create(request);
    request.step.input = { amount: 750 };
    request.evidence![0].summary = "Changed proposal";
    resolveKey("test-key");
    const result = await pending;
    // Then: the transmitted and returned decision describe the original proposal.
    expect((await requests[0].json()).step.input).toEqual({ amount: 450 });
    expect(result.request.evidence[0].summary).toBe("Original proposal");
  });

  it("retains explicit policy choices without turning a denial into permission", async () => {
    const { client } = harness(async (request) => json({ ...receipt(await request.json()),
      decision: { outcome: "deny", reason: "Policy denied", check: {
        execution_contract: "hosted-v1", allowed: false, confidence: 1, matches: [], non_matches: [], errors: [], unresolved: [],
      } } }));
    const result = await client.operations.create({ ...proposal, errorPolicy: "block", unresolvedPolicy: "allow" });
    expect(result.decision.outcome).toBe("deny");
    expect(result.request.errorPolicy).toBe("block");
    expect(result.request.unresolvedPolicy).toBe("allow");
  });

  it.each([401, 403, 409, 422, 429, 500, 503])("does not retry HTTP %d and preserves Retry-After", async (status) => {
    const { client, requests } = harness(async () => json({ detail: "server-only detail" }, status, { "Retry-After": "1" }));
    await expect(client.operations.create(proposal)).rejects.toMatchObject({ name: "NormaAPIError", statusCode: status, retryAfter: "1" });
    expect(requests).toHaveLength(1);
  });

  it.each(["network", "timeout", "json", "missing safety", "coerced safety", "unknown decision"])("holds on %s failure", async (kind) => {
    const { client, requests } = harness(async () => {
      if (kind === "network") throw new TypeError("fetch failed");
      if (kind === "timeout") throw new DOMException("Timeout", "TimeoutError");
      if (kind === "json") return new Response("not json", { status: 200 });
      const result = receipt();
      return json({ ...result, decision: { ...result.decision,
        outcome: kind === "unknown decision" ? "approved" : "allow",
        check: { ...result.decision.check, execution_contract: "hosted-v1", allowed: kind === "missing safety" ? undefined : "true" },
      } });
    });
    await expect(client.operations.create(proposal)).rejects.toThrow();
    expect(requests).toHaveLength(1);
  });

  it.each(["key", "agent", "target", "proposal"])("rejects a mismatched %s receipt", async (kind) => {
    const result = receipt();
    const { client } = harness(async () => json({ ...result,
      operation_key: kind === "key" ? "other-key" : result.operation_key,
      agent_name: kind === "agent" ? "different-agent" : AGENT,
      target_type: kind === "target" ? "project" : null,
      target_id: kind === "target" ? "other" : null,
      request: kind === "proposal" ? { ...wireRequest, step: { ...wireRequest.step, input: { price_usd: 750 } } } : wireRequest,
    }));
    await expect(client.operations.create(proposal)).rejects.toThrow(/does not match/);
  });

  it.each([
    { ...proposal, agentName: "other-agent" },
    { ...proposal, operationKey: "" },
    { ...proposal, step: { ...proposal.step, name: "" } },
    { ...proposal, step: { ...proposal.step, type: "llm" } },
    { ...proposal, step: { ...proposal.step, output: "already executed" } },
    { ...proposal, step: { ...proposal.step, groundTruth: "reference" } },
    { ...proposal, step: { ...proposal.step, context: { norma_operation_evidence: [] } } },
    { ...proposal, step: { ...proposal.step, input: Number.NaN } },
    { ...proposal, step: { ...proposal.step, input: new Date() } },
    { ...proposal, step: { ...proposal.step, input: { oversized: "x".repeat(65537) } } },
  ])("rejects invalid proposals before credential resolution or transport", async (invalid) => {
    const agentKey = vi.fn(async () => "test-key");
    const { client, requests } = harness(async () => json(receipt()), { agentKey });
    await expect(client.operations.create(invalid as CreateOperation)).rejects.toThrow();
    expect(requests).toEqual([]);
    expect(agentKey).not.toHaveBeenCalled();
  });

  it("binds get/list to the configured agent and target and checks returned IDs", async () => {
    const scoped = { ...receipt({ ...wireRequest, target_type: "project", target_id: "space/id" }),
      target_type: "project", target_id: "space/id" };
    const { client, requests } = harness(async (request) => json(
      new URL(request.url).pathname.endsWith(`/${operationId}`) ? scoped : { operations: [scoped] },
    ), { targetType: "project", targetId: "space/id" });
    expect((await client.operations.get(operationId)).id).toBe(operationId);
    expect(await client.operations.list({ limit: 3 })).toHaveLength(1);
    for (const request of requests) {
      const url = new URL(request.url);
      expect(request.method).toBe("GET");
      expect(url.searchParams.get("agent_name")).toBe(AGENT);
      expect(url.searchParams.get("target_type")).toBe("project");
      expect(url.searchParams.get("target_id")).toBe("space/id");
    }
    expect(new URL(requests[1].url).searchParams.get("limit")).toBe("3");
    const wrong = harness(async () => json({ ...receipt(), id: "other-id" }));
    await expect(wrong.client.operations.get(operationId)).rejects.toThrow(/does not match/);
  });

  it("records caller reports separately from trusted verification and frozen decisions", async () => {
    const reported = { ...receipt(), observations: [{ id: "report-1", event_key: "effect-1", kind: "succeeded",
      summary: "Caller wrote the local record", external_reference: "outbox-1", source: "caller_reported",
      recorded_at: "2026-10-09T19:01:00Z" }], current_outcome: "succeeded" };
    const { client, requests } = harness(async () => json(reported));
    const result = await client.operations.observe(operationId, { eventKey: "effect-1", kind: "succeeded",
      summary: "Caller wrote the local record", externalReference: "outbox-1" });
    expect(requests[0].url.endsWith(`/operations/${operationId}/observations`)).toBe(true);
    expect(await requests[0].json()).toEqual({ agent_name: AGENT, event_key: "effect-1", kind: "succeeded",
      summary: "Caller wrote the local record", external_reference: "outbox-1" });
    expect(result.observations[0].source).toBe("caller_reported");
    expect(result.verifications).toEqual([]);
    expect(result.decision.outcome).toBe("allow");
  });

  it("links the existing verification identity without submitting invented assessments", async () => {
    const verified = { ...receipt(), verifications: [{ session_id: "verification-1", revision: 1,
      claim: "postcondition_assessed", linked_at: "2026-10-09T19:01:00Z", assessment: {
        revision: 1, subject_version: "1", definition_fingerprint: "definition", fingerprint: "assessment",
        outcome: "unknown", claim_scope: "correlated_external_effect", criteria: [], observed_facts: [],
        inferred_judgment: null, satisfied_criteria: [], unsatisfied_criteria: ["record-present"],
        assessed_at: "2026-10-09T19:01:00Z",
      } }] };
    const { client, requests } = harness(async () => json(verified));
    const result = await client.operations.linkVerification(operationId, "verification-1");
    expect(await requests[0].json()).toEqual({ agent_name: AGENT, verification_session_id: "verification-1" });
    expect(result.verifications[0].assessment.outcome).toBe("unknown");
    expect(result.verifications[0].claim).toBe("postcondition_assessed");
    expect(result.currentOutcome).toBe("not_observed");
  });

  it("preserves opaque JSON keys and drops internal provider provenance", async () => {
    const request = { ...wireRequest, step: { ...wireRequest.step, input: JSON.parse('{"arbitrary_key":{"type":"statement","support_score":0.8},"__proto__":{"kept":true}}') } };
    const response = receipt(request);
    const { client } = harness(async () => json({ ...response, decision: { ...response.decision, provider_models: { semantic: "private-pin" } } }));
    const result = await client.operations.create({ ...proposal, step: { ...proposal.step, input: request.step.input } });
    expect(result.request.step.input).toEqual(request.step.input);
    expect(JSON.stringify(result)).not.toContain("private-pin");
  });

  it.each(["unsafe", "deny", "steer", "error", "unresolved"])("rejects a contradictory allowed %s receipt", async (kind) => {
    const result = receipt();
    const match = { action: kind === "deny" || kind === "steer" ? kind : "observe", rule_id: 1,
      rule_name: "scope-guard", result: { matched: false, confidence: 0 } };
    const { client } = harness(async () => json({ ...result, decision: { ...result.decision,
      check: { ...result.decision.check, execution_contract: "hosted-v1", allowed: false,
        matches: kind === "deny" || kind === "steer" ? [match] : [],
        errors: kind === "error" ? [match] : [], unresolved: kind === "unresolved" ? [match] : [],
      },
    } }));
    await expect(client.operations.create(proposal)).rejects.toThrow(/contradicts/);
  });

  it("preserves an explicit observe result even with allowed false", async () => {
    const result = receipt();
    const { client } = harness(async () => json({ ...result, decision: { ...result.decision,
      check: { ...result.decision.check, execution_contract: "hosted-v1", allowed: false, matches: [{ action: "observe", rule_id: 1,
        rule_name: "watch-only", result: { matched: true, confidence: 1 } }] },
    } }));
    expect((await client.operations.create(proposal)).decision).toMatchObject({outcome: "allow", allowed: true});
  });

  it("rejects traversal IDs and transport overrides without making a request", async () => {
    const { client, requests } = harness(async () => json(receipt()));
    await expect(client.operations.get("..")).rejects.toThrow();
    await expect(client.operations.get(operationId, { serverURL: "https://other.invalid" } as never)).rejects.toThrow();
    expect(requests).toEqual([]);
  });

  it.each([null, [], "scalar", 12])("rejects non-object tool input %j locally", async (input) => {
    const { client, requests } = harness(async () => json(receipt()));
    await expect(client.operations.create({ ...proposal, step: { ...proposal.step, input } } as unknown as CreateOperation)).rejects.toThrow();
    expect(requests).toEqual([]);
  });

  it.each([false, "error"])("rejects malformed observe evidence %j", async (kind) => {
    const result = receipt();
    const { client } = harness(async () => json({ ...result, decision: { ...result.decision,
      check: { ...result.decision.check, execution_contract: "hosted-v1", allowed: false, matches: [{ action: "observe", rule_id: 1,
        rule_name: "watch-only", result: { matched: kind !== false, confidence: 1, error: kind === "error" ? "Unavailable" : null } }] },
    } }));
    await expect(client.operations.create(proposal)).rejects.toThrow(/contradicts/);
  });

  it("rejects unchanged receipt acknowledgments for observation and verification", async () => {
    const { client } = harness(async () => json(receipt()));
    await expect(client.operations.observe(operationId, { eventKey: "event-1", kind: "succeeded", summary: "Recorded" })).rejects.toThrow(/does not acknowledge/);
    await expect(client.operations.linkVerification(operationId, "verification-1")).rejects.toThrow(/does not acknowledge/);
  });

  it("does not dispatch after cancellation during asynchronous authentication", async () => {
    let release!: (value: string) => void;
    const key = new Promise<string>((resolve) => { release = resolve; });
    const { client, requests } = harness(async () => json(receipt()), { agentKey: () => key });
    const controller = new AbortController();
    const pending = client.operations.create(proposal, { signal: controller.signal });
    controller.abort();
    release("synthetic-key");
    await expect(pending).rejects.toThrow();
    expect(requests).toEqual([]);
  });

  it("repeated creates submit the same key and never automatically call an effect", async () => {
    const { client, requests } = harness(async () => json(receipt()));
    const first = await client.operations.create(proposal);
    const repeated = await client.operations.create(proposal);
    expect(first.id).toBe(repeated.id);
    expect(requests).toHaveLength(2);
    expect(await requests[0].json()).toEqual(await requests[1].json());
  });

  it("requires initialization and exposes typed HTTP errors", () => {
    expect(() => new NormaClient().operations).toThrow(/not initialized/);
    expect(new NormaAPIError(409, "1")).toMatchObject({ code: "NORMA_CONFLICT", retryAfter: "1" });
  });
});

describe("current shared operation receipt fixture", () => {
  it("decodes all retained rules, caller evidence and independent assessments", async () => {
    const fixture = (await import("./fixtures/operation-receipt.json")).default;
    const { client } = harness(async () => json(fixture));
    const result = await client.operations.get(fixture.id);
    expect(result.rules[0]).toMatchObject({ ruleId: 12, version: 3, disposition: "evaluated" });
    expect(result.rules[0].definition.condition.evaluator?.config).toEqual({ field_constraints: { price_usd: { max: 500 } } });
    expect(result.observations[0].source).toBe("caller_reported");
    expect(result.verifications[0].assessment).toMatchObject({ outcome: "unknown", claimScope: "correlated_external_effect",
      observedFacts: [{ evidenceId: "outbox", origin: "caller_asserted", status: "untrusted" }],
      unsatisfiedCriteria: ["record-present"] });
  });

  it("requires dual acknowledgement and preserves frozen control evidence on operation readback", async () => {
    const receiptValue = {
      contract: "rule-controls-v1", capabilities_fingerprint: "a".repeat(64), declaration_fingerprint: "b".repeat(64),
      defaults: { age: 90 }, effective_values: { age: 120 }, values_fingerprint: "c".repeat(64),
      value_sources: { age: "agent_override" }, references: [], overridden_keys: ["age"],
      agent_generation: "generation-1", agent_name: AGENT, rule_id: 7, rule_version: 3, override_revision: 4,
      effective_fingerprint: "d".repeat(64),
    };
    const frozenEvidence = { schema_version: 1, completeness: "incomplete", agent_name: AGENT,
      agent_generation: "generation-1", rule_id: 7, rule_version: 3, definition_fingerprint: "d".repeat(64),
      receipt: receiptValue, references: [] };
    const value = receipt();
    value.control_contract = "rule-controls-v1";
    value.agent_generation = "generation-1";
    value.decision.check = { ...value.decision.check, control_contract: "rule-controls-v1",
      agent_generation: "generation-1", control_resolutions: [receiptValue], control_evidence: [frozenEvidence] };
    const { client, requests } = harness(async () => json(value, 200, { "X-Norma-Control-Contract": "rule-controls-v1" }), {
      controlContract: "rule-controls-v1",
    });
    const result = await client.operations.get(operationId);
    expect(requests[0]?.headers.get("X-Norma-Control-Contract")).toBe("rule-controls-v1");
    expect(result.controlContract).toBe("rule-controls-v1");
    expect(result.agentGeneration).toBe("generation-1");
    expect(result.decision.controlEvidence?.[0]?.receipt?.effectiveValues).toEqual({ age: 120 });
  });
});

describe("hosted operation enforcement", () => {
  it("rejects fail-open error policy before transport", async () => {
    const { client, requests } = harness(async () => json(receipt()));
    // @ts-expect-error Untyped callers cannot weaken the hosted error policy.
    await expect(client.operations.create({ ...proposal, errorPolicy: "allow" })).rejects.toThrow();
    expect(requests).toHaveLength(0);
  });
  it("rejects a newly created SDK-local rule receipt", async () => {
    const fixture = structuredClone((await import("./fixtures/operation-receipt.json")).default);
    fixture.rules[0].definition.execution = "sdk";
    const { client } = harness(async () => json(fixture));
    await expect(client.operations.create(proposal)).rejects.toThrow(/SDK-local rule snapshot/);
  });
});

describe("hosted operation admission proof", () => {
  it.each([undefined, "unknown-v2"])("rejects create response proof %s", async (proof) => {
    // Given: a runtime returns a structurally valid but unproven retained decision.
    const { client } = harness(async () => {
      const value = receipt();
      return json({ ...value, decision: { ...value.decision, check: {
        ...value.decision.check, execution_contract: proof,
      } } });
    });
    // When/Then: creating a new proposal cannot turn it into execution permission.
    await expect(client.operations.create(proposal)).rejects.toThrow(/hosted-v1/);
  });
  it("can inspect historical receipts that predate hosted proof", async () => {
    // Given: historical records are evidence, not fresh permission to execute.
    const { client } = harness(async () => {
      const value = receipt();
      return json({ ...value, decision: { ...value.decision, check: {
        ...value.decision.check, execution_contract: undefined,
      } } });
    });
    // When/Then: reading preserves the prior decision and its missing proof.
    const record = await client.operations.get(operationId);
    expect(record.decision).not.toHaveProperty("check");
    expect(record.decision.allowed).toBe(true);
    expect(record.currentOutcome).toBe("not_observed");
  });
});

describe("retained operation permission semantics", () => {
  it("does not turn a historical permissive error policy into safe permission", async () => {
    const value = receipt({...wireRequest, error_policy: "allow"});
    const {client} = harness(async () => json({...value, decision: {...value.decision, check: {
      ...value.decision.check, allowed: false, errors: [{action: "observe", rule_id: 1, rule_name: "old-check",
        result: {matched: false, confidence: 0, error: "Unavailable"}}],
    }}}));
    const result = await client.operations.get(operationId);
    expect(result.decision).toMatchObject({outcome: "allow", allowed: false});
  });
  it("permits an explicit unresolved observe policy without ignoring effective deny", async () => {
    let effective = "observe";
    const value = receipt({...wireRequest, unresolved_policy: "allow"});
    const {client} = harness(async () => json({...value, decision: {...value.decision, check: {
      ...value.decision.check, allowed: false, unresolved: [{action: "observe", effective_action: effective,
        rule_id: 1, rule_name: "watch", result: {matched: false, confidence: 0, outcome: "unresolved"}}],
    }}}));
    expect((await client.operations.create({...proposal, unresolvedPolicy: "allow"})).decision).toMatchObject({outcome: "allow", allowed: true});
    effective = "deny";
    await expect(client.operations.create({...proposal, unresolvedPolicy: "allow"})).rejects.toThrow(/contradicts/);
  });
});
