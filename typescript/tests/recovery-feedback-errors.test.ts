// Modified for Norma.
import { checkFor } from "../src/client";
import { readFileSync } from "node:fs";
import { afterEach, describe, expect, it, vi } from "vitest";

import { CheckError, RuleViolationError, NormaClient, checkpoint } from "../src/index";
import { RecoveryFeedbackSchema } from "../src/internal/semantic-check";

const wire = JSON.parse(readFileSync(new URL("./fixtures/recovery-feedback.json", import.meta.url), "utf8"));
const safe = { execution_contract: "hosted-v1", allowed: true, confidence: 1 };

function initializedClient(): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "feedback-test",  runtimeUrl: "https://test.invalid", agentKey: "synthetic" });
  return client;
}

function responseFor(kind: string) {
  const response = structuredClone(wire);
  const blocking = response.matches[1];
  if (kind === "steer") blocking.action = "steer";
  if (kind.startsWith("unresolved")) {
    response.matches = [];
    response.unresolved = [blocking];
    blocking.effective_action = kind.replace("unresolved-", "");
    blocking.result = { ...blocking.result, matched: false, outcome: "unresolved" };
    Object.assign(response.recovery_feedback[1], { outcome: "unresolved", reason: "missing_evidence" });
  }
  if (kind === "error") {
    // Keep deny/observe matches to prove evaluator errors still win.
    response.errors = [{ ...blocking, check_id: "execution-error", result: {
      matched: false, confidence: 0, error: "Synthetic outage",
    } }];
    Object.assign(response.recovery_feedback[1], { outcome: "error", reason: "check_error" });
  }
  return response;
}

function jsonResponse(value: unknown): Response {
  return new Response(JSON.stringify(value), { headers: { "content-type": "application/json" } });
}

describe("structured recovery feedback on public exceptions", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it.each(["pre", "post"] as const)("retains every field at %s without retries", async (stage) => {
    for (const kind of ["deny", "steer", "unresolved-deny", "unresolved-steer", "error"]) {
      // Given: the same synthetic wire fixture as Python, through the real HTTP codec.
      const response = responseFor(kind);
      const http = vi.fn();
      if (stage === "post") http.mockResolvedValueOnce(jsonResponse(safe));
      http.mockResolvedValueOnce(jsonResponse(response));
      vi.stubGlobal("fetch", http);
      const stub = vi.fn(async (proposal: string) => proposal);
      const wrapped = checkpoint(stub, { client: initializedClient(), stepName: "proposal-check" });

      // When: a public wrapper receives deny, steer, unresolved, or evaluator error.
      const error = await wrapped("harmless synthetic draft").catch((failure: unknown) => failure);

      // Then: exact feedback is exposed, including unrelated first records and leaf evidence.
      expect(error).toBeInstanceOf(kind === "error" ? CheckError : RuleViolationError);
      if (!(error instanceof CheckError || error instanceof RuleViolationError)) throw error;
      expect(error.stage).toBe(stage);
      expect(error.recoveryFeedback).toEqual(response.recovery_feedback.map((item: unknown) => RecoveryFeedbackSchema.parse(item)));
      expect(error.recoveryFeedback?.[1]).toMatchObject({
        conditionIds: ["reviewed"], conditionPath: "root.1.not", conditionOutcome: "not_matched", negated: true,
        observationId: "observation-synthetic", definitionFingerprint: "definition-synthetic", evidenceFingerprint: "bundle-synthetic",
        evidence: [{ path: "input.proposal", origin: "caller_asserted", sourceId: "synthetic-proposal", sourceVersion: "revision-1" }],
      });
      if (error instanceof CheckError) {
        expect(error.checkIds).toEqual(["execution-error"]);
      } else {
        expect(error.checkId).toBe("execution-blocking");
        expect(error.ruleId).toBe("7");
        expect(error.action).toBe(kind.endsWith("steer") ? "steer" : "deny");
      }
      expect(stub).toHaveBeenCalledTimes(stage === "pre" ? 0 : 1);
      expect(http.mock.calls.filter(([request]) => (request as Request).url.endsWith("/checks")))
        .toHaveLength(stage === "pre" ? 1 : 2);
      expect(await (http.mock.calls[0][0] as Request).clone().json()).toMatchObject({
        stage: "pre", step: { input: { args: ["harmless synthetic draft"] } },
      });
    }
  });

  it.each(["deny", "steer", "error"])("supports a legacy %s response without fabricating identifiers", async (kind) => {
    // Given: an older server sends only the decision.
    const response = responseFor(kind);
    delete response.recovery_feedback;
    for (const item of [...response.matches, ...response.errors]) delete item.check_id;
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(response)));
    const stub = vi.fn(async () => "effect");
    // When: the public wrapper enforces it.
    const error = await checkpoint(stub, { client: initializedClient() })().catch((failure: unknown) => failure);
    // Then: old exception behavior remains and new facts stay absent.
    expect(error).toBeInstanceOf(kind === "error" ? CheckError : RuleViolationError);
    if (!(error instanceof CheckError || error instanceof RuleViolationError)) throw error;
    expect(error.recoveryFeedback).toBeUndefined();
    expect(error.stage).toBe("pre");
    expect(error instanceof CheckError ? error.checkIds : error.checkId).toBeUndefined();
    expect(stub).not.toHaveBeenCalled();
  });

  it("uses only public Rule constructor fields", () => {
    // Given / When: the public constructor shape.
    const error = new RuleViolationError({ ruleName: "checkpoint", ruleId: "7", action: "deny", decision: {allowed: false, reason: null, checks: []} });
    // Then: Rule identifiers remain and optional context stays absent.
    expect(error.message).toBe("Rule violation: checkpoint");
    expect(error.stage).toBeUndefined();
    expect(error.recoveryFeedback).toBeUndefined();
    expect(error.checkId).toBeUndefined();
  });

  it("reports transport stage/cause without feedback and never runs the action", async () => {
    // Given: the transport fails before a response exists.
    const client = initializedClient();
    const cause = new Error("Synthetic outage");
    vi.spyOn(checkFor(client), "check").mockRejectedValue(cause);
    const stub = vi.fn(async () => "effect");
    // When: the public wrapper checks the action.
    const error = await checkpoint(stub, { client })().catch((failure: unknown) => failure);
    // Then: Error compatibility and fail-closed behavior are preserved.
    expect(error).toBeInstanceOf(CheckError);
    if (!(error instanceof CheckError)) throw error;
    expect(error.stage).toBe("pre");
    expect(error.cause).toBe(cause);
    expect(error.recoveryFeedback).toBeUndefined();
    expect(error.checkIds).toBeUndefined();
    expect(stub).not.toHaveBeenCalled();
  });
});
