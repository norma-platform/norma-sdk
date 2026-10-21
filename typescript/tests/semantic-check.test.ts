// Modified for Norma.
import { checkFor } from "../src/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NormaClient, checkpoint, RuleViolationError } from "../src/index";

function client(): NormaClient {
  const active = new NormaClient();
  active.init({ agentName: "quote-agent",  runtimeUrl: "http://localhost:8000", agentKey: "test" });
  return active;
}

const observation = {
  observation_id: "observation-1", status: "completed",
  answers: {
    bounded_scope: { type: "choice", choice: "within_package", confidence: 0.9, probabilities: { within_package: 0.823456789, outside_package: 0.176543211 } },
    evidence_exists: { type: "statement", support_score: 0.712345678 },
    quality_score: { type: "score", score: 1, confidence: 0.8, legend: { "0": "weak", "1": "strong" }, probabilities: { "0": 0.2, "1": 0.8 } },
  },
  rule_outcomes: { bounded_scope: "not_matched", evidence_exists: "matched", quality_score: "unresolved" },
  evidence: {
    items: [{
      name: "quote", path: "input.quote", value: { price_usd: 450, arbitrary_key: [null, true] },
      required: true, origin: "runtime_observed", status: "present", source_id: "run-1",
      source_version: "3", observed_at: "2026-10-09T12:00:00Z", fingerprint: "item-fingerprint", truncated: false,
    }],
    fingerprint: "bundle-fingerprint", complete: false, missing_required: ["approval"], unresolved: ["approval"],
  },
  definition_fingerprint: "definition-fingerprint", question_fingerprint: "question-fingerprint",
  evidence_fingerprint: "bundle-fingerprint", check_release: "semantic-test",
  latency_ms: 12.3, usage: { input_tokens: 123, output_tokens: 45 },
};

function match(executionId: string) {
  return {
    action: "observe", effective_action: "deny", rule_id: 7, rule_name: "quote-evidence",
    check_id: executionId,
    result: {
      confidence: 0.9, matched: false, outcome: "unresolved", semantic_observation: observation,
      failed_condition_ids: ["0.1"], unresolved_evidence_ids: ["approval"], unresolved_action: "deny",
      message: "Approval evidence missing", metadata: { custom_wire_key: "unchanged" },
    },
  };
}

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
}

describe("semantic check HTTP contract", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("retains semantic answers, evidence, recovery feedback and execution identity in every result bucket", async () => {
    // Given: a server wire response including all current semantic fields.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      confidence: 0.9, execution_contract: "hosted-v1", allowed: false,
      matches: [match("matched-execution")], non_matches: [match("nonmatched-execution")],
      errors: [{ ...match("error-execution"), result: { matched: false, confidence: 0, error: "Provider unavailable" } }],
      unresolved: [match("unresolved-execution")],
      recovery_feedback: [{
        rule_id: 7, check_id: "unresolved-execution", outcome: "unresolved", reason: "missing_evidence",
        condition_ids: ["0.1"], condition_path: "0.1", condition_outcome: "unresolved", negated: false,
        observation_id: "observation-1", definition_fingerprint: "definition-fingerprint", evidence_fingerprint: "bundle-fingerprint",
        evidence: [{ binding: "approval", path: "context.approval", required: true, unresolved: true, status: "missing", origin: "caller_asserted", source_id: null, source_version: null, fingerprint: "missing-fingerprint" }],
      }],
    })));

    // When: the public SDK reads it through the actual generated HTTP codec.
    const response = await checkFor(client()).check({ agentName: "quote-agent", stage: "pre", step: { type: "tool", name: "send", input: {} } });

    // Then: documented camelCase fields preserve exact decisions, identities and raw numbers.
    expect([response.matches?.[0].checkId, response.nonMatches?.[0].checkId, response.errors?.[0].checkId, response.unresolved?.[0].checkId]).toEqual([
      "matched-execution", "nonmatched-execution", "error-execution", "unresolved-execution",
    ]);
    for (const item of [response.matches?.[0], response.nonMatches?.[0], response.unresolved?.[0]]) {
      expect(item).toMatchObject({ effectiveAction: "deny", result: {
        outcome: "unresolved", unresolvedAction: "deny", failedConditionIds: ["0.1"], unresolvedEvidenceIds: ["approval"],
        metadata: { custom_wire_key: "unchanged" },
      } });
      expect(item?.result.semanticObservation).toEqual({
        observationId: "observation-1", status: "completed", answers: { ...observation.answers, evidence_exists: {type: "statement", support_score: observation.answers.evidence_exists.support_score} },
        ruleOutcomes: observation.rule_outcomes, definitionFingerprint: "definition-fingerprint",
        questionFingerprint: "question-fingerprint", evidenceFingerprint: "bundle-fingerprint",
        checkRelease: "semantic-test", latencyMs: 12.3, usage: { inputTokens: 123, outputTokens: 45 },
        evidence: {
          fingerprint: "bundle-fingerprint", complete: false, missingRequired: ["approval"], unresolved: ["approval"],
          items: [{ name: "quote", path: "input.quote", value: observation.evidence.items[0].value,
            required: true, origin: "runtime_observed", status: "present", sourceId: "run-1", sourceVersion: "3",
            observedAt: "2026-10-09T12:00:00Z", fingerprint: "item-fingerprint", truncated: false }],
        },
      });
    }
    expect(response.errors?.[0].result.error).toBe("Provider unavailable");
    expect(response.recoveryFeedback).toEqual([{
      ruleId: 7, checkId: "unresolved-execution", outcome: "unresolved", reason: "missing_evidence",
      conditionIds: ["0.1"], conditionPath: "0.1", conditionOutcome: "unresolved", negated: false,
      observationId: "observation-1", definitionFingerprint: "definition-fingerprint", evidenceFingerprint: "bundle-fingerprint",
      evidence: [{ binding: "approval", path: "context.approval", required: true, unresolved: true, status: "missing", origin: "caller_asserted", sourceId: null, sourceVersion: null, fingerprint: "missing-fingerprint" }],
    }]);
  });

  it.each(["deny", "steer", "observe"] as const)("enforces the unresolved %s action received over HTTP", async (effectiveAction) => {
    // Given: configured observe differs from the action required for missing evidence.
    const fetchMock = vi.fn(async () => jsonResponse({
      confidence: 0, execution_contract: "hosted-v1", allowed: effectiveAction === "observe", matches: [], errors: [],
      unresolved: [{ ...match("unresolved-execution"), effective_action: effectiveAction }],
    }));
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async () => "ran");
    // When: invoking the public guarded function through the real HTTP codec.
    const result = checkpoint(execute, { client: client() })();
    // Then: blocking actions retain their rule identity; observe remains nonblocking.
    if (effectiveAction === "observe") {
      await expect(result).resolves.toBe("ran");
      expect(execute).toHaveBeenCalledTimes(1);
      expect(fetchMock.mock.calls.filter(([request]) => (request as Request).url.endsWith("/checks"))).toHaveLength(2);
    } else {
      await expect(result).rejects.toBeInstanceOf(RuleViolationError);
      await expect(result).rejects.toMatchObject({ action: effectiveAction, ruleId: "7", ruleName: "quote-evidence" });
      expect(execute).not.toHaveBeenCalled();
      expect(fetchMock.mock.calls.filter(([request]) => (request as Request).url.endsWith("/checks"))).toHaveLength(1);
    }
  });

  it("fails closed on malformed semantic evidence rather than dropping it", async () => {
    // Given: a nominally safe response contains a probability outside the shared contract.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      confidence: 1, execution_contract: "hosted-v1", allowed: true, non_matches: [{ ...match("bad-evidence"), result: {
        ...match("bad-evidence").result, semantic_observation: { ...observation, answers: { bad: { type: "statement", statement: 2 } } },
      } }],
    })));
    const execute = vi.fn(async () => "ran");
    // When: evaluating through the public wrapper and generated transport.
    await expect(checkpoint(execute, { client: client() })()).rejects.toThrow(/Norma check failed during pre/);
    // Then: the invalid result cannot authorize execution.
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("strict safety boolean over the HTTP codec", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
  it.each(["true", "false", null, undefined, 1, {}, []])("blocks malformed allowed=%j before execution", async (allowed) => {
    // Given: JSON that the generated default boolean codec might coerce.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({
      confidence: 1, execution_contract: "hosted-v1", allowed: allowed, matches: [], non_matches: [], errors: [],
    })));
    const execute = vi.fn(async () => "ran");
    // When: invoking the public wrapper through the actual generated HTTP codec.
    await expect(checkpoint(execute, { client: client() })()).rejects.toThrow(/Norma check failed during pre/);
    // Then: malformed safety evidence never authorizes execution.
    expect(execute).not.toHaveBeenCalled();
  });
});

describe("opaque retained evidence keys", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
  it("preserves own prototype-named evidence and answer keys without mutation", async () => {
    const answers = JSON.parse('{"__proto__":{"type":"statement","support_score":0.73}}');
    const value = JSON.parse('{"__proto__":{"observed_key":true},"arbitrary_key":450}');
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ confidence: 1, execution_contract: "hosted-v1", allowed: true,
      non_matches: [{ ...match("literal-keys"), result: { matched: false, confidence: 1,
        semantic_observation: { ...observation, answers,
          evidence: { ...observation.evidence, items: [{ ...observation.evidence.items[0], value }] },
        },
      } }],
    })));
    const result = await checkFor(client()).check({ agentName: "quote-agent", stage: "pre",
      step: { type: "tool", name: "inspect", input: {} } });
    const retained = result.nonMatches?.[0].result.semanticObservation;
    expect(retained?.answers).toEqual(JSON.parse('{"__proto__":{"type":"statement","support_score":0.73}}'));
    expect(answers["__proto__"].type).toBe("statement");
    expect(Object.hasOwn(retained!.answers!, "__proto__")).toBe(true);
    expect(retained?.evidence.items?.[0].value).toEqual(value);
    expect(Object.getPrototypeOf(retained!.answers!)).toBe(Object.prototype);
  });
});
