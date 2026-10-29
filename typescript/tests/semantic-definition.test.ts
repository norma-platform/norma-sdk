// Modified for Norma.
import { afterEach, describe, expect, expectTypeOf, it, vi } from "vitest";

import {
  fromRuntimeSemanticDefinition, semanticDefinition, toNormaSemanticAnswer,
  toRuntimeSemanticAnswer, toRuntimeSemanticDefinition,
  type NormaSemanticDefinitionInput,
  type SemanticDefinitionConfig,
} from "../src/internal/semantic-definition";
import contract from "./fixtures/semantic-definition-contract.json";

function definition(): NormaSemanticDefinitionInput {
  return {
    definition_id: "quote-check", check_release: "customer-release",
    bindings: [{ name: "quote", path: "input.quote" }],
    questions: [{ id: "bounded", type: "statement", instructions: "Are terms bounded?",
      criteria: { true: "Bounded terms", false: "Unbounded terms" } }],
    decision: { on_unresolved: "deny", rules: [{ type: "statement", question_id: "bounded", threshold: 0.8 }] },
  };
}

describe("Norma semantic authoring contract", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("accepts compact authoring while leaving identity and managed release to the server", () => {
    // Given: a question supplies its type once; operational policy remains explicit.
    const compact: NormaSemanticDefinitionInput = {
      bindings: [{ name: "quote", path: "input.quote", required: false,
        required_origin: "source_verified", max_chars: 100 }],
      questions: [{ id: "bounded", type: "statement", instructions: "Are terms bounded?",
        criteria: { true: "Bounded terms", false: "Unbounded terms" } }],
      decision: { operator: "any", on_unresolved: "steer", rules: [{
        question_id: "bounded", threshold: 0.7, match_when: "at_most", ambiguous_band: [0.6, 0.8],
      }] },
    };

    // When: preparing authoring through the exported semantic helper.
    const output = semanticDefinition(compact);

    // Then: defaults do not replace explicit policy, source data, or server-owned identity.
    expect(output).not.toHaveProperty("definition_id");
    expect(output).not.toHaveProperty("check_release");
    expect(output.bindings[0]).toMatchObject(compact.bindings[0]);
    expect(output.decision).toEqual({ operator: "any", on_unresolved: "steer", rules: [{
      type: "statement", question_id: "bounded", threshold: 0.7, match_when: "at_most",
      ambiguous_band: [0.6, 0.8],
    }] });
    expect(compact.decision.rules[0]).not.toHaveProperty("type");
  });

  it.each(["explicit mismatch", "missing question", "duplicate question", "null type"])(
    "does not infer over an invalid semantic reference: %s", (scenario) => {
      // Given: an invalid authored reference must not acquire a guessed type.
      const input = definition();
      const rule = { question_id: "bounded", threshold: 0.8 };
      if (scenario === "explicit mismatch") Object.assign(rule, { type: "score", min_score: 0 });
      if (scenario === "missing question") rule.question_id = "unknown";
      if (scenario === "duplicate question") input.questions.push({ ...input.questions[0] });
      if (scenario === "null type") Object.assign(rule, { type: null });
      input.decision.rules = [rule];

      // When / Then: public validation rejects instead of weakening the rule.
      expect(() => semanticDefinition(input)).toThrow();
    },
  );

  for (const fixture of contract.cases) {
    it(`matches the shared model: ${fixture.name}`, () => {
      // Given: a case and canonical output derived from the shared Pydantic model.
      const parse = () => fromRuntimeSemanticDefinition(fixture.input);
      // When/Then: the facade accepts and round-trips exactly the same JSON, or rejects it.
      if (!fixture.valid) {
        expect(parse).toThrow();
      } else {
        const publicView = parse();
        expect(toRuntimeSemanticDefinition(publicView)).toEqual(fixture.normalized);
        expect(semanticDefinition(publicView)).toEqual(publicView);
      }
    });
  }

  it("uses the same native vocabulary in authoring and runtime views", () => {
    const input = definition();
    input.examples = [{state: {}, expected_answers: {bounded: {type: "statement", support_score: 0.75}}}];
    const native = toRuntimeSemanticDefinition(input);
    expect(fromRuntimeSemanticDefinition(native)).toEqual(native);
    expect([native].map(fromRuntimeSemanticDefinition)).toEqual([native]);
    const answer = {type: "statement", support_score: 0.75} as const;
    expect([answer].map(toNormaSemanticAnswer)).toEqual([answer]);
    expect(toNormaSemanticAnswer(answer)).not.toBe(answer);
  });

  it("applies shared defaults while keeping the caller's explicit uncertainty action", () => {
    // Given: public truth authoring with no inferred action or provider configuration.
    const input = definition();
    input.decision.on_unresolved = "observe";
    // When: validating the public form and converting explicitly for the runtime.
    const publicView = semanticDefinition(input);
    const wire = toRuntimeSemanticDefinition(publicView);
    // Then: only shared defaults are added, and the wire vocabulary is unchanged.
    expect(publicView.schema_version).toBe("1");
    expect(publicView.definition_version).toBe(1);
    expect(publicView.decision.operator).toBe("all");
    expect(publicView.decision.on_unresolved).toBe("observe");
    expect(wire.questions[0].type).toBe("statement");
    expect(wire.decision.rules[0]).toEqual({ type: "statement", question_id: "bounded",
      threshold: 0.8, match_when: "at_least", ambiguous_band: null });
    expect(input).not.toHaveProperty("schema_version");
    expectTypeOf(wire).toEqualTypeOf<SemanticDefinitionConfig>();
  });

  it("converts only known nodes and returns detached JSON, including special object keys", () => {
    // Given: arbitrary example state deliberately containing both vocabularies.
    const input = definition();
    const state = JSON.parse('{"type":"truth","value":0.4,"nested":[{"type":"statement","support_score":0.2}],"__proto__":{"type":"truth","value":0.3}}');
    input.examples = [{ state, expected_answers: { bounded: { type: "statement", support_score: 0.723456789 } } }];
    // When: converting public -> runtime -> public.
    const wire = toRuntimeSemanticDefinition(input);
    const restored = fromRuntimeSemanticDefinition(wire);
    // Then: example state is exact data, and changing any result cannot mutate its source.
    expect(JSON.stringify(wire.examples[0].state)).toBe(JSON.stringify(state));
    expect(JSON.stringify(restored.examples[0].state)).toBe(JSON.stringify(state));
    expect(wire.examples[0].expected_answers.bounded).toEqual({ type: "statement", support_score: 0.723456789 });
    expect(restored.examples[0].expected_answers.bounded).toEqual({ type: "statement", support_score: 0.723456789 });
    wire.examples[0].state.value = 0.99;
    expect(state.value).toBe(0.4);
    expect(restored.examples[0].state.value).toBe(0.4);
  });

  it("preserves valid author-defined __proto__ candidate and answer identifiers", () => {
    // Given: identifiers allowed by the shared model, treated as own JSON properties.
    const input = definition();
    input.questions = [{ id: "__proto__", type: "choice", instructions: "Pick one",
      criteria: JSON.parse('{"__proto__":"First","other":"Second"}') }];
    input.decision.rules = [{ type: "choice", question_id: "__proto__", match_choices: ["__proto__"] }];
    input.examples = [{ state: {}, expected_answers: JSON.parse('{"__proto__":{"type":"choice","choice":"__proto__","confidence":1,"probabilities":{"__proto__":1,"other":0}}}') }];
    // When/Then: conversion retains every candidate and answer without prototype mutation.
    const output = toRuntimeSemanticDefinition(input);
    expect(Object.hasOwn(output.examples[0].expected_answers, "__proto__")).toBe(true);
    expect(output.questions[0].criteria).toEqual(input.questions[0].criteria);
    expect(Object.getPrototypeOf(output.examples[0].expected_answers)).toBe(Object.prototype);
  });

  it("retains the measured truth value and returns detached choice/score answers", () => {
    // Given: numeric measurements, not inferred authorization or prose.
    const truth = { type: "statement", support_score: 0.7123456789 } as const;
    const choice = { type: "choice", choice: "yes", confidence: 0.9,
      probabilities: { yes: 0.823456789, no: 0.176543211 } } as const;
    const score = { type: "score", score: 0.75, confidence: 0.8,
      legend: { "0": "Low", "1": "High" }, probabilities: { "0": 0.25, "1": 0.75 } } as const;
    // When/Then: only the truth tag/value key changes.
    expect(toNormaSemanticAnswer(toRuntimeSemanticAnswer(truth))).toEqual(truth);
    for (const answer of [choice, score]) {
      const wire = toRuntimeSemanticAnswer(answer);
      expect(wire).toEqual(answer);
      expect(wire).not.toBe(answer);
      expect(toNormaSemanticAnswer(wire)).toEqual(answer);
    }
  });

  it.each([undefined, Number.NaN, Infinity, -0, 1n, new Date(), new Map(), () => 1])(
    "rejects unsupported nested author data (%s) instead of changing it during serialization", (value) => {
      // Given: untrusted JSON input containing a JavaScript-only value.
      const fixture = contract.cases.find((item) => item.valid);
      const input = { ...fixture?.input, examples: [{ state: { value }, expected_answers: {} }] };
      // When/Then: parsing fails before the value can become an evaluator configuration.
      expect(() => fromRuntimeSemanticDefinition(input)).toThrow();
    },
  );

  it("rejects accessors without running them, cycles and sparse arrays", () => {
    // Given: non-data objects that JSON serialization would execute, lose, or reject.
    const getter = vi.fn(() => "secret");
    const accessor = Object.defineProperty({}, "definition_id", { get: getter, enumerable: true });
    const cycle: Record<string, unknown> = {};
    cycle.self = cycle;
    // When/Then: the safe boundary rejects all three without invoking application code.
    for (const input of [accessor, cycle, { ...definition(), examples: Array(2) }]) {
      expect(() => fromRuntimeSemanticDefinition(input)).toThrow();
    }
    expect(getter).not.toHaveBeenCalled();
  });

  it("rejects public typos and legacy tags while preserving Unicode instruction bounds", () => {
    // Given: dynamic input can still contain invalid properties despite static typing.
    const input = definition();
    input.questions[0].instructions = "😀".repeat(16000);
    expect(semanticDefinition(input).questions[0].instructions).toBe(input.questions[0].instructions);
    // When/Then: invalid public syntax never produces an executable wire config.
    expect(() => semanticDefinition({ ...input, ...{ provider: "private" } })).toThrow();
    expect(() => toNormaSemanticAnswer({ type: "unknown", support_score: 0.5 })).toThrow();
    expect(() => toNormaSemanticAnswer({ type: "statement", support_score: 1.01 })).toThrow();
    expect(() => toRuntimeSemanticAnswer({ type: "statement", support_score: 0.5, ...{ confidence: 0.9 } })).toThrow();
  });

  it("does not coerce policy numbers", () => {
    const runtimeInput = toRuntimeSemanticDefinition(definition());
    expect(fromRuntimeSemanticDefinition(runtimeInput)).toEqual(runtimeInput);
    // @ts-expect-error Numeric measurements must not be coerced from strings.
    expect(() => toRuntimeSemanticAnswer({type: "statement", support_score: "0.5"})).toThrow();
    expect(() => fromRuntimeSemanticDefinition({...runtimeInput, decision: {
      ...runtimeInput.decision, rules: [{type: "statement", question_id: "bounded", threshold: "0.8"}],
    }})).toThrow();
  });
});
