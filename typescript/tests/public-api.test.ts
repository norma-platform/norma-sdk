// Modified for Norma.
import { afterEach, describe, expect, it, vi } from "vitest";
import { NormaClient, HTTPClient, NormaAPIError, checkpoint, RuleViolationError, RuleSteerError, CheckError,
  type StepSchema } from "../src/index";

const json = (data: unknown, status = 200, headers = {}) => new Response(JSON.stringify(data), {
  status, headers: { "content-type": "application/json", ...headers },
});
function client(fetcher: (request: Request) => Promise<Response>, controlContract?: "rule-controls-v1"): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "public-agent", agentKey: "synthetic-test-key", runtimeUrl: "https://norma.invalid", observabilityEnabled: false, controlContract,
    httpClient: new HTTPClient({ fetcher }) });
  return client;
}
afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); });

describe("Norma setup and vocabulary", () => {

  it("preserves a frozen controlled-rule receipt through public list and register APIs", async () => {
    const receipt = {
      contract: "rule-controls-v1", capabilities_fingerprint: "a".repeat(64),
      declaration_fingerprint: "b".repeat(64), defaults: { age: 90 }, effective_values: { age: 120 },
      values_fingerprint: "c".repeat(64), value_sources: { age: "agent_override" },
      references: [{ key: "age", source_pointer: "/controls/age/default",
        destination_pointer: "/condition/evaluator/config/field_constraints/age/min",
        evaluator_pointer: "/condition/evaluator", slot: { control_types: ["number"] } }],
      overridden_keys: ["age"], agent_generation: "generation-1", agent_name: "public-agent",
      rule_id: 7, rule_version: 3, override_revision: 4, effective_fingerprint: "d".repeat(64),
    };
    const payload = {
      rules: [{ id: 7, name: "age-check", rule: { execution: "server", enabled: true, mode: "enforce",
        action: { decision: "deny" }, condition: { selector: { path: "*" },
          evaluator: { name: "json", config: { field_constraints: { age: { min: 120 } } } } } } }],
      control_contract: "rule-controls-v1", agent_generation: "generation-1", control_resolutions: [receipt],
      created: false, overwrite_applied: false,
    };
    const fetcher = vi.fn(async (request: Request) => {
      expect(request.headers.get("X-Norma-Control-Contract")).toBe("rule-controls-v1");
      return json(payload, 200, { "X-Norma-Control-Contract": "rule-controls-v1" });
    });
    const sdk = client(fetcher, "rule-controls-v1");

    const listed = await sdk.agents.listRules({ agentName: "public-agent" });
    const registered = await sdk.agents.register({ agent: { agentName: "public-agent" } });

    for (const result of [listed, registered]) {
      expect(result.controlContract).toBe("rule-controls-v1");
      expect(result.agentGeneration).toBe("generation-1");
      expect(result.controlResolutions?.[0]?.effectiveValues).toEqual({ age: 120 });
      expect(Object.isFrozen(result.controlResolutions?.[0]?.effectiveValues)).toBe(true);
      expect(Object.isFrozen(result.controlResolutions?.[0])).toBe(true);
    }
    expect(listed.rules).toHaveLength(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls.every(([request]) => request.headers.get("X-Norma-Control-Contract") === "rule-controls-v1")).toBe(true);
  });

  it("keeps legacy headerless agent-rule reads free of synthetic control state", async () => {
    const sdk = client(async () => json({ rules: [] }));
    const listed = await sdk.agents.listRules({ agentName: "public-agent" });
    expect(listed).toEqual({ rules: [] });
  });

  it("rejects a malformed control receipt in the public agent-rule response", async () => {
    const malformed = {
      rules: [], control_contract: "rule-controls-v1", agent_generation: "generation-1",
      control_resolutions: [{ contract: "rule-controls-v1", capabilities_fingerprint: "a".repeat(64),
        declaration_fingerprint: "b".repeat(64), defaults: { age: 90 }, effective_values: { age: 120 },
        values_fingerprint: "c".repeat(64), value_sources: { age: "agent_override" }, references: [],
        overridden_keys: ["age"], agent_generation: "generation-1", agent_name: "public-agent",
        rule_id: 7, rule_version: 3, override_revision: "4", effective_fingerprint: "d".repeat(64) }],
    };
    const sdk = client(async () => json(malformed, 200, {
      "X-Norma-Control-Contract": "rule-controls-v1",
    }), "rule-controls-v1");

    await expect(sdk.agents.listRules({ agentName: "public-agent" })).rejects.toThrow();
  });


  it("uses the actual registration StepSchema and defaults to strict", async () => {
    // Given: public typed step declarations for an existing allowed identity.
    const fetcher = vi.fn(async () => json({ created: false }));
    const step: StepSchema = { type: "tool", name: "lookup", inputSchema: { type: "object" } };
    // When: declaring steps using the default registration mode.
    await client(fetcher).agents.register({ agent: { agentName: "public-agent" }, steps: [step] });
    // Then: no replacement permission is requested by default.
    expect(await fetcher.mock.calls[0][0].json()).toMatchObject({ conflict_mode: "strict", steps: [{ type: "tool", name: "lookup" }] });
  });

  it("requires configured checkpoints and retains exact compatibility error aliases", async () => {
    // Given: the new checkpoint public entry and an unconfigured client.
    const execute = vi.fn(async () => "effect");
    // When / Then: a missing setup cannot silently execute protected work.
    await expect(checkpoint(execute, { client: new NormaClient() })()).rejects.toThrow(/not initialized/);
    expect(execute).not.toHaveBeenCalled();
  });

  it.each(["deny", "steer"])("raises the public %s error before executing", async (action) => {
    const sdk = client(async () => json({ confidence: 1, execution_contract: "hosted-v1", allowed: false, matches: [{ rule_id: 7, rule_name: "Approval required", check_id: "check-7", action,
      result: { confidence: 1, matched: true, message: "Ask for approval." } }] }));
    const execute = vi.fn(async () => "effect");
    const error = await checkpoint(execute, { client: sdk })().catch((failure: unknown) => failure);
    expect(error).toBeInstanceOf(action === "steer" ? RuleSteerError : RuleViolationError);
    expect(error).toMatchObject({ ruleId: "7", ruleName: "Approval required", checkId: "check-7" });
    expect(execute).not.toHaveBeenCalled();
  });


  it("returns product check names with the same denied decision", async () => {
    const sdk = client(async () => json({ confidence: 1, execution_contract: "hosted-v1", allowed: false, matches: [{ rule_id: 42, rule_name: "Block word", action: "deny", result: { confidence: 1, matched: true } }] }));
    const decision = await sdk.checks.check({ stage: "pre", step: { type: "tool", name: "lookup", input: {} } });
    expect(decision.allowed).toBe(false);
    expect(decision.checks).toEqual([expect.objectContaining({ ruleId: 42, ruleName: "Block word", status: "matched" })]);
    expect(decision.checks[0]).toHaveProperty("ruleId", 42);
  });
});

describe("one public HTTP failure contract", () => {
  it.each(["registration", "operation", "checkpoint"])("preserves auth denial through %s", async (path) => {
    // Given: a rejected request with old branding and an internal permission in its payload.
    const sdk = client(async () => json({ type: "https://private.invalid/errors/auth-insufficient-privileges", error_code: "AUTH_INSUFFICIENT_PRIVILEGES", detail: "Not authorized agents.create synthetic-private" }, 403,
      { "x-request-id": "0123456789abcdef0123456789abcdef", "retry-after": "2" }));
    const execute = vi.fn(async () => "effect");
    const request = path === "registration" ? sdk.agents.register({ agent: { agentName: "public-agent" } })
      : path === "operation" ? sdk.operations.create({ operationKey: "test", step: { type: "tool", name: "lookup", input: {} } })
      : checkpoint(execute, { client: sdk })();
    // When: inspecting the public error without parsing a raw HTTP response.
    const error = await request.catch((failure: unknown) => failure);
    // Then: denial, retry and correlation survive, without inherited prose or execution.
    expect(error).toBeInstanceOf(path === "checkpoint" ? CheckError : NormaAPIError);
    expect(error).toMatchObject({ code: "NORMA_ACCESS_DENIED", statusCode: 403, retryAfter: "2", requestId: "0123456789abcdef0123456789abcdef" });
    expect(String(error)).not.toMatch(/private-runtime|agents\.create|synthetic-private/i);
    expect(execute).not.toHaveBeenCalled();
  });
});


it.each(["matches", "unresolved", "errors"])("observe mode keeps %s diagnostic and permits execution", async (bucket) => {
  const sdk = client(async () => json({confidence: 1, allowed: true, execution_contract: "hosted-v1",
    [bucket]: [{rule_id: 7, rule_name: "Observed", check_id: "check-7", action: "steer", mode: "observe", effective_action: "observe",
      steering_context: {message: "Preserve guidance"}, result: {matched: bucket === "matches", confidence: 1,
        error: bucket === "errors" ? "synthetic error" : null}}]}));
  const execute = vi.fn(async () => "effect");
  await expect(checkpoint(execute, {client: sdk})()).resolves.toBe("effect");
  expect(execute).toHaveBeenCalledOnce();
  const decision = await sdk.checks.check({stage: "pre", step: {type: "tool", name: "example", input: {}}});
  expect(decision.checks[0]).toMatchObject({mode: "observe", action: "steer", steeringContext: {message: "Preserve guidance"}});
});
