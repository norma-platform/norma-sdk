// Modified for Norma.
import { checkFor } from "../src/client";
import { describe, expect, it, vi } from "vitest";

import { NormaClient } from "../src/client";
import { checkpoint } from "../src/checkpoint";
import { RuleViolationError } from "../src/errors";

function initializedClient(): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "coding-agent",  runtimeUrl: "http://localhost:8000", agentKey: "test" });
  return client;
}

const safe = { confidence: 1, allowed: true, matches: [], nonMatches: [], errors: [] };
const deny = {
  confidence: 1,
  allowed: false,
  matches: [{
    action: "deny",
    ruleId: 7,
    ruleName: "human-approval",
    result: { confidence: 1, matched: true, message: "Ask the owner first" },
  }],
  nonMatches: [],
  errors: [],
};

describe("checkpoint", () => {
  it("rejects an uninitialized checkpoint before any action", async () => {
    const execute = vi.fn(async (value: string) => `echo:${value}`);
    const wrapped = checkpoint(execute);
    await expect(wrapped("hello")).rejects.toThrow(/not initialized/);
    expect(execute).not.toHaveBeenCalled();
  });

  it("evaluates pre and post with truthful generic action context", async () => {
    const client = initializedClient();
    const evaluate = vi.spyOn(checkFor(client), "check").mockResolvedValue(safe);
    const execute = vi.fn(async (command: string) => `ran:${command}`);
    const wrapped = checkpoint(execute, { client, stepName: "native.exec" });

    await expect(wrapped("ls")).resolves.toBe("ran:ls");
    expect(evaluate).toHaveBeenCalledTimes(2);
    expect(evaluate.mock.calls[0]?.[0]).toMatchObject({
      agentName: "coding-agent", stage: "pre",
      step: { type: "tool", name: "native.exec", input: { args: ["ls"] }, context: { norma_action: { kind: "other", target: null, command: null, cwd: null } } },
    });
    expect(evaluate.mock.calls[1]?.[0]).toMatchObject({ stage: "post", step: { output: "ran:ls" } });
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("uses only caller-provided shell action facts and denies before execution", async () => {
    const client = initializedClient();
    const evaluate = vi.spyOn(checkFor(client), "check").mockResolvedValue(deny);
    const execute = vi.fn(async (command: string) => command);
    const wrapped = checkpoint(execute, {
      client, stepName: "tools.shell",
      action: (command: string) => ({ kind: "shell", target: "/repo", command, cwd: "/repo" }),
    });

    await expect(wrapped("rm -rf scratch")).rejects.toBeInstanceOf(RuleViolationError);
    expect(execute).not.toHaveBeenCalled();
    expect(evaluate.mock.calls[0]?.[0].step.context).toEqual({
      norma_action: { kind: "shell", target: "/repo", command: "rm -rf scratch", cwd: "/repo" },
      norma_argument_shape: "positional",
    });
  });

  it("fails closed on check errors before execution", async () => {
    const client = initializedClient();
    vi.spyOn(checkFor(client), "check").mockRejectedValue(new Error("network down"));
    const execute = vi.fn(async () => "ran");
    await expect(checkpoint(execute, { client })()).rejects.toThrow(/Norma check failed/i);
    expect(execute).not.toHaveBeenCalled();
  });

  it("checks post execution and reports a server evaluator failure", async () => {
    const client = initializedClient();
    vi.spyOn(checkFor(client), "check")
      .mockResolvedValueOnce(safe)
      .mockResolvedValueOnce({ ...safe, errors: [{
        action: "observe", ruleId: 9, ruleName: "runtime-error",
        result: { confidence: 0, matched: false, error: "provider unavailable" },
      }] });
    const execute = vi.fn(async () => "ran");
    await expect(checkpoint(execute, { client })()).rejects.toThrow(/Norma check failed.*post/i);
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("blocks unresolved deny, but keeps unresolved observe nonblocking", async () => {
    const client = initializedClient();
    const unresolved = { confidence: 0, allowed: false, matches: [], errors: [], unresolved: [{
      action: "observe", effectiveAction: "deny", ruleId: 11, ruleName: "workspace-check", result: { matched: false, confidence: 0, message: "Path unknown" },
    }] };
    const evaluate = vi.spyOn(checkFor(client), "check").mockResolvedValueOnce(unresolved);
    const execute = vi.fn(async () => "ran");
    await expect(checkpoint(execute, { client })()).rejects.toBeInstanceOf(RuleViolationError);
    expect(execute).not.toHaveBeenCalled();
    expect(evaluate).toHaveBeenCalledTimes(1);
  });

  it("forwards an explicitly scoped target pair to both checks", async () => {
    const client = initializedClient();
    const evaluate = vi.spyOn(checkFor(client), "check").mockResolvedValue(safe);
    await checkpoint(async () => "ran", { client, targetType: "project", targetId: "repo-1" })();
    expect(evaluate.mock.calls.map(([request]) => [request.targetType, request.targetId])).toEqual([
      ["project", "repo-1"], ["project", "repo-1"],
    ]);
  });
});
