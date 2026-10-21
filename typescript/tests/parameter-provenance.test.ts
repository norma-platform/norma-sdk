import { describe, expect, it } from "vitest";
import { RuleDefinitionOutput$inboundSchema } from "../src/generated/models/rule-definition-output";
import { ruleDefinitionFromWire } from "../src/internal/public-adapters";

describe("frozen shared parameter receipts", () => {
  it("preserves the server snapshot without new instrumentation arguments", () => {
    const parameters = {contract: "policy-parameters-v1", set: "documents", revision: 1,
      values: {max_words: 100}, schema_fingerprint: "a".repeat(64), values_fingerprint: "b".repeat(64)};
    const parsed = RuleDefinitionOutput$inboundSchema.parse({execution: "server",
      condition: {selector: {path: "*"}, evaluator: {name: "regex", config: {pattern: "x"}}},
      action: {decision: "deny"}, parameters});
    expect(ruleDefinitionFromWire(parsed)?.parameters).toEqual(parameters);
  });
});
