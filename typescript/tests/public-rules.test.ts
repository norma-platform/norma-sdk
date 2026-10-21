// Modified for Norma.
import { describe, expect, it, vi } from "vitest";
import * as sdk from "../src/index";
import { readFileSync } from "node:fs";

const json = (value: unknown) => new Response(JSON.stringify(value), {headers: {"content-type": "application/json"}});
function client(response: unknown) {
  const instance = new sdk.NormaClient();
  instance.init({agentName: "quotation-agent", runtimeUrl: "https://tenant.example", agentKey: "synthetic",
    observabilityEnabled: false, httpClient: new sdk.HTTPClient({fetcher: async () => json(response)})});
  return instance;
}
const match = {rule_id: 12, rule_name: "quote-rule", check_id: "check-1", action: "observe",
  result: {matched: false, confidence: 1}};
const check = {stage: "pre" as const, step: {name: "quote", type: "tool", input: {}}};
const assertPublic = (value: unknown): void => {
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value)) {
    expect(["raw", "namespaceKey"]).not.toContain(key);
    assertPublic(item);
  }
};
describe("public Rules contract", () => {
  it("exports native APIs without raw transport escape hatches", () => {
    const instance = client({});
    expect(instance.checks.check).toBeTypeOf("function");
    expect(instance.agents.listRules).toBeTypeOf("function");
    expect(instance.agents.register).toBeTypeOf("function");
    for (const name of ["toRuntimeSemanticDefinition", "toRuntimeSemanticAnswer", "toNormaSemanticAnswer", "fromRuntimeSemanticDefinition"]) expect(name in sdk).toBe(false);
  });
  it("invalidates a previous identity when reconfiguration fails", async () => {
    const instance = client({execution_contract: "hosted-v1", allowed: true, confidence: 1});
    expect(() => instance.init({agentName: "another-agent", runtimeUrl: "invalid", agentKey: "synthetic"})).toThrow();
    expect(instance.initialized).toBe(false);
    const effect = vi.fn(async () => "effect");
    await expect(sdk.checkpoint(effect, {client: instance})()).rejects.toThrow(/not initialized/);
    expect(effect).not.toHaveBeenCalled();
  });
  it("returns only normalized Decision identifiers", async () => {
    const decision = await client({execution_contract: "hosted-v1", allowed: true, confidence: 1, non_matches: [match]}).checks.check(check);
    expect(decision.allowed).toBe(true);
    expect(decision.checks[0]).toMatchObject({ruleId: 12, ruleName: "quote-rule", checkId: "check-1", status: "not_matched"});
    assertPublic(decision);
  });
  it.each(["matches", "non_matches", "unresolved"])("fails closed on an error hidden in %s", async bucket => {
    const instance = client({execution_contract: "hosted-v1", allowed: true, confidence: 1,
      [bucket]: [{...match, result: {...match.result, error: "Evaluator unavailable"}}]});
    expect((await instance.checks.check(check)).allowed).toBe(false);
    const effect = vi.fn(async () => "effect");
    await expect(sdk.checkpoint(effect, {client: instance})()).rejects.toBeInstanceOf(sdk.CheckError);
    expect(effect).not.toHaveBeenCalled();
  });
  it("maps agent rule lists and rejects local definitions as hosted authoring", async () => {
    const definition = {execution: "server", enabled: true, action: {decision: "deny"}, condition: {selector: {path: "input"}, evaluator: {name: "json", config: {}}}};
    const result = await client({rules: [{id: 12, name: "hosted", rule: definition},
      {id: 13, name: "historical", rule: {...definition, execution: "sdk"}}]}).agents.listRules({agentName: "quotation-agent"});
    expect(result.rules[0]).toMatchObject({status: "hosted", definition: {execution: "server"}});
    expect(result.rules[1]).toMatchObject({status: "unsupported", definition: null});
    assertPublic(result);
  });
  it("normalizes semantic rule definitions only at recognized protocol fields", async () => {
    const config = {questions: [{id: "q", type: "statement", instructions: "Retain statement text"}],
      decision: {rules: [{type: "statement", question_id: "q", threshold: 0.7}]},
      examples: [{state: {type: "statement", statement: "caller data"}, expected_answers: {q: {type: "statement", support_score: 0.8}}}]};
    const definition = {execution: "server", enabled: true, action: {decision: "observe"},
      condition: {and: [{selector: {path: "input"}, evaluator: {name: "semantic", config}}]}};
    const result = await client({rules: [{id: 12, name: "meaning", rule: definition}]}).agents.listRules({agentName: "quotation-agent"});
    const mapped = result.rules[0].definition!.condition.and![0].evaluator!.config;
    expect(mapped).toMatchObject({questions: [{type: "statement", instructions: "Retain statement text"}],
      decision: {rules: [{type: "statement"}]}, examples: [{state: {type: "statement", statement: "caller data"}, expected_answers: {q: {type: "statement", support_score: 0.8}}}]});
    expect(config.questions[0].type).toBe("statement");
  });
  it("maps bindings without exposing runtime namespaces", async () => {
    const binding = {rule_id: 12, id: 1, namespace_key: "private", enabled: true, target_type: "project", target_id: "p",
      created_at: "2026-10-19T00:00:00Z", updated_at: "2026-10-19T00:00:00Z"};
    const value = await client(binding).ruleBindings.get({bindingId: 1});
    expect(value.ruleId).toBe(12); expect(value).not.toHaveProperty("namespaceKey"); assertPublic(value);
  });
  it("normalizes retained operation receipts without raw check access", async () => {
    const fixture = JSON.parse(readFileSync(new URL("./fixtures/operation-receipt.json", import.meta.url), "utf8"));
    const instance = new sdk.NormaClient();
    instance.init({agentName: fixture.agent_name, runtimeUrl: "https://tenant.example", agentKey: "synthetic",
      httpClient: new sdk.HTTPClient({fetcher: async () => json(fixture)})});
    const result = await instance.operations.get(fixture.id);
    expect(result.rules[0].ruleId).toBe(12);
    expect(result.decision).not.toHaveProperty("check"); assertPublic(result);
    fixture.decision.check.non_matches = [{...match, result: {...match.result, error: "Unavailable"}}];
    fixture.decision.outcome = "allow";
    await expect(instance.operations.get(fixture.id)).rejects.toThrow(/contradicts/);
  });
});
