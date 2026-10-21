// Modified for Norma.
import { describe, expect, it, vi } from "vitest";
import { CheckError, NormaClient, RuleSteerError, RuleViolationError, checkpoint } from "../src";
import type { JsonObject } from "../src";
import { checkFor } from "../src/client";

const safe = { confidence: 1, allowed: true, matches: [], nonMatches: [], errors: [] };

function clientWithChecks() {
  const client = new NormaClient();
  client.init({ agentName: "inventory-agent", runtimeUrl: "https://example.invalid/api/runtime", agentKey: "synthetic" });
  const checks = vi.spyOn(checkFor(client), "check").mockResolvedValue(safe);
  return { client, checks };
}

describe("checkpoint observed state", () => {
  it("captures inline state at invocation before yielding and reuses it after the action", async () => {
    // Given: existing values need no callback or database lookup.
    const { client, checks } = clientWithChecks();
    const state = { inventory: { available: 9 }, entity: "order-a" };
    const execute = vi.fn(async (quantity: number) => {
      state.inventory.available = 0;
      return quantity;
    });
    const wrapped = checkpoint(execute, { client, state });
    // When: the owner mutates values while the invocation is pending.
    const pending = wrapped(2);
    state.entity = "order-b";
    await pending;
    // Then: both Checks see exactly the captured values, with unchanged arguments.
    expect(checks.mock.calls.map(([request]) => ({
      input: request.step.input,
      state: request.step.context?.state,
      stage: request.stage,
    }))).toEqual([
      { input: { args: [2] }, state: { inventory: { available: 9 }, entity: "order-a" }, stage: "pre" },
      { input: { args: [2] }, state: { inventory: { available: 9 }, entity: "order-a" }, stage: "post" },
    ]);
    expect(checks.mock.calls[0][0].step.context?.state).not.toBe(checks.mock.calls[1][0].step.context?.state);
    expect(execute).toHaveBeenCalledOnce();
  });

  it("binds callback state once per invocation with concurrent users on one agent", async () => {
    // Given: two in-flight calls resolve their own snapshots in reverse order.
    const { client, checks } = clientWithChecks();
    let releaseFirst!: (value: JsonObject) => void;
    const first = new Promise<JsonObject>(resolve => { releaseFirst = resolve; });
    const state = vi.fn((entity: string) => entity === "a" ? first : { entity, available: 4 });
    const wrapped = checkpoint(async (entity: string) => entity, { client, state });
    // When: the second action finishes before the first snapshot resolves.
    const a = wrapped("a");
    await expect(wrapped("b")).resolves.toBe("b");
    releaseFirst({ entity: "a", available: 8 });
    await expect(a).resolves.toBe("a");
    // Then: snapshots never cross invocation boundaries.
    expect(state.mock.calls).toEqual([["a"], ["b"]]);
    expect(checks.mock.calls.map(([r]) => [r.stage, r.step.input, r.step.context?.state])).toEqual([
      ["pre", { args: ["b"] }, { entity: "b", available: 4 }],
      ["post", { args: ["b"] }, { entity: "b", available: 4 }],
      ["pre", { args: ["a"] }, { entity: "a", available: 8 }],
      ["post", { args: ["a"] }, { entity: "a", available: 8 }],
    ]);
  });

  it("uses new values on a subsequent invocation without refreshing postcheck state", async () => {
    // Given: an application-managed mutable source is deliberately supplied.
    const { client, checks } = clientWithChecks();
    const source = { available: 10 };
    const state = vi.fn(() => source);
    const wrapped = checkpoint(async () => { source.available--; return null; }, { client, state });
    // When: two sequential invocations run.
    await wrapped();
    await wrapped();
    // Then: capture occurs once per invocation, not once per wrapper or stage.
    expect(state).toHaveBeenCalledTimes(2);
    expect(checks.mock.calls.map(([r]) => r.step.context?.state)).toEqual([
      { available: 10 }, { available: 10 }, { available: 9 }, { available: 9 },
    ]);
  });

  it.each(["sync", "async"])("blocks a %s callback failure without exposing its message", async mode => {
    // Given: the application callback fails with sensitive exception text.
    const { client, checks } = clientWithChecks();
    const failure = () => { throw new Error("private database token"); };
    const state = mode === "sync" ? failure : async () => failure();
    const execute = vi.fn(async () => null);
    // When: capture fails.
    const error = await checkpoint(execute, { client, state })().catch(error => error);
    // Then: no Check or action runs, and the error contains no callback details.
    expect(error).toBeInstanceOf(CheckError);
    expect(error.stage).toBe("pre");
    expect(error.message).not.toContain("private database token");
    expect(error.cause).toBeUndefined();
    expect(checks).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });

  it.each([
    ["array root", []],
    ["null root", null],
    ["undefined value", { value: undefined }],
    ["NaN", { value: NaN }],
    ["infinity", { value: Infinity }],
    ["negative zero", { value: -0 }],
    ["unsafe integer", { value: Number.MAX_SAFE_INTEGER + 1 }],
    ["date", { value: new Date(0) }],
    ["sparse array", { value: new Array(2) }],
    ["bigint", { value: 1n }],
    ["symbol key", { [Symbol("hidden")]: "private" }],
  ])("rejects %s before either Check or effect", async (_label, invalid) => {
    // Given: state is not a supported explicit JSON snapshot.
    const { client, checks } = clientWithChecks();
    const execute = vi.fn(async () => null);
    // When: capturing through the public checkpoint.
    await expect(checkpoint(execute, { client, state: invalid as JsonObject })()).rejects.toBeInstanceOf(CheckError);
    // Then: nothing was sent or executed.
    expect(checks).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });

  it("never invokes accessors or toJSON while validating state", async () => {
    // Given: serialization hooks could expose application data.
    const { client, checks } = clientWithChecks();
    const getter = vi.fn(() => "secret");
    const toJSON = vi.fn(() => ({ secret: true }));
    const accessor = Object.defineProperty({}, "secret", { enumerable: true, get: getter });
    const execute = vi.fn(async () => null);
    // When: either object is offered as state.
    for (const state of [accessor, { toJSON }]) {
      await expect(checkpoint(execute, { client, state: state as JsonObject })()).rejects.toBeInstanceOf(CheckError);
    }
    // Then: neither hook ran.
    expect(getter).not.toHaveBeenCalled();
    expect(toJSON).not.toHaveBeenCalled();
    expect(checks).not.toHaveBeenCalled();
  });

  it("rejects cycles but accepts shared acyclic values as independent JSON copies", async () => {
    // Given: JSON cannot represent a cycle.
    const { client, checks } = clientWithChecks();
    const cyclic: JsonObject = {};
    cyclic.self = cyclic;
    // When: capture encounters a cycle and then a shared noncyclic object.
    await expect(checkpoint(async () => null, { client, state: cyclic })()).rejects.toBeInstanceOf(CheckError);
    const shared = { count: 2 };
    await checkpoint(async () => null, { client, state: { a: shared, b: shared } })();
    // Then: shared values survive exactly.
    expect(checks.mock.calls[0][0].step.context?.state).toEqual({ a: { count: 2 }, b: { count: 2 } });
  });

  it("enforces the exact UTF-8 JSON byte limit", async () => {
    // Given: {"text":""} costs eleven bytes, and multibyte text counts encoded bytes.
    const { client, checks } = clientWithChecks();
    await checkpoint(async () => null, { client, state: { text: "x".repeat(65_536 - 11) } })();
    checks.mockClear();
    // When: either ASCII or multibyte data exceeds the limit.
    for (const text of ["x".repeat(65_536 - 10), "é".repeat(32_763)]) {
      await expect(checkpoint(async () => null, { client, state: { text } })()).rejects.toBeInstanceOf(CheckError);
    }
    // Then: oversized state is not transmitted.
    expect(checks).not.toHaveBeenCalled();
  });

  it("bounds nesting and total nodes including object keys", async () => {
    // Given: the root has depth zero, and containers, values, and object keys count as nodes.
    const { client, checks } = clientWithChecks();
    const nested = (depth: number): JsonObject => {
      let result: JsonObject = {};
      for (let i = 0; i < depth; i++) result = { child: result };
      return result;
    };
    await checkpoint(async () => null, { client, state: nested(32) })();
    await checkpoint(async () => null, { client, state: { values: Array(9_997).fill(0) } })();
    checks.mockClear();
    // When: exceeding either limit by one.
    for (const state of [nested(33), { values: Array(9_998).fill(0) }]) {
      await expect(checkpoint(async () => null, { client, state })()).rejects.toBeInstanceOf(CheckError);
    }
    // Then: capture fails before transmission.
    expect(checks).not.toHaveBeenCalled();
  });

  it("adds state to an LLM checkpoint without changing its input shape", async () => {
    // Given: existing generic LLM checkpoints use an array input.
    const { client, checks } = clientWithChecks();
    // When: observed state is added.
    await checkpoint(async (prompt: string) => prompt, { client, stepType: "llm", state: { locale: "en" } })("hello");
    // Then: only context gains the explicit snapshot.
    expect(checks.mock.calls[0][0].step).toMatchObject({ input: ["hello"], context: { state: { locale: "en" } } });
    expect(checks.mock.calls.map(([r]) => r.step.context?.norma_argument_shape)).toEqual([undefined, undefined]);
  });

  it("marks positional tool envelopes explicitly for prechecks and postchecks", async () => {
    // Given: a named application field called args must not be mistaken for this SDK's envelope.
    const { client, checks } = clientWithChecks();
    const argument = { args: ["application-owned"], quantity: 3 };
    // When: the generic TypeScript tool checkpoint supplies state.
    await checkpoint(async (value: typeof argument) => value, { client, state: { available: 5 } })(argument);
    // Then: input stays unchanged and both stages identify the positional SDK envelope.
    expect(checks.mock.calls.map(([r]) => ({
      stage: r.stage, input: r.step.input,
      shape: r.step.context?.norma_argument_shape,
      state: r.step.context?.state,
    }))).toEqual([
      { stage: "pre", input: { args: [argument] }, shape: "positional", state: { available: 5 } },
      { stage: "post", input: { args: [argument] }, shape: "positional", state: { available: 5 } },
    ]);
  });

  it.each(["deny", "steer", "observe"] as const)("preserves %s decision behavior with state", async action => {
    // Given: state does not change existing decision enforcement.
    const { client, checks } = clientWithChecks();
    checks.mockResolvedValue({ ...safe, allowed: action === "observe", matches: [{
      action, ruleId: 1, ruleName: "inventory", result: { confidence: 1, matched: true },
    }] });
    const execute = vi.fn(async () => "done");
    // When: a Rule matches.
    const result = checkpoint(execute, { client, state: { available: 0 } })();
    // Then: observe executes, deny and steer block.
    if (action === "observe") {
      await expect(result).resolves.toBe("done");
      expect(execute).toHaveBeenCalledOnce();
    } else {
      await expect(result).rejects.toBeInstanceOf(action === "deny" ? RuleViolationError : RuleSteerError);
      expect(execute).not.toHaveBeenCalled();
    }
  });

  it.each(["deny", "steer", "observe"] as const)("blocks returned %s evaluator errors before the action", async action => {
    // Given: hosted runtime allowed alone is not sufficient when a Check has an error.
    const { client, checks } = clientWithChecks();
    checks.mockResolvedValue({ ...safe, errors: [{
      action, ruleId: 1, ruleName: "inventory", result: { confidence: 0, matched: false, error: "evaluation failed" },
    }] });
    const execute = vi.fn(async () => null);
    // When: the runtime returns an evaluator error.
    await expect(checkpoint(execute, { client, state: {} })()).rejects.toBeInstanceOf(CheckError);
    // Then: all action modes preserve the SDK's error boundary.
    expect(execute).not.toHaveBeenCalled();
  });

  it("blocks an unavailable runtime after capture without executing the action", async () => {
    // Given: a state callback succeeds but the hosted check is unavailable.
    const { client, checks } = clientWithChecks();
    checks.mockRejectedValue(new Error("unavailable"));
    const state = vi.fn(() => ({ available: 9 }));
    const execute = vi.fn(async () => null);
    // When: the precheck fails.
    await expect(checkpoint(execute, { client, state })()).rejects.toMatchObject({ name: "CheckError", stage: "pre" });
    // Then: state was resolved once and the effect never ran.
    expect(state).toHaveBeenCalledOnce();
    expect(execute).not.toHaveBeenCalled();
  });
});
