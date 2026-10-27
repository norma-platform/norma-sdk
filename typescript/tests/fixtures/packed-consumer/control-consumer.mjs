import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { checkFinish, HTTPClient, NormaClient, validateAuthoredControls } from "norma-sdk";

const receipt = {
  contract: "rule-controls-v1", capabilities_fingerprint: "a".repeat(64),
  declaration_fingerprint: "b".repeat(64), defaults: { age: 90 }, effective_values: { age: 120 },
  values_fingerprint: "c".repeat(64), value_sources: { age: "agent_override" },
  references: [{ key: "age", source_pointer: "/controls/age/default",
    destination_pointer: "/condition/evaluator/config/field_constraints/age/min",
    evaluator_pointer: "/condition/evaluator", slot: { control_types: ["number"] } }],
  overridden_keys: ["age"], agent_generation: "generation-1", agent_name: "controls-agent",
  rule_id: 7, rule_version: 3, override_revision: 4, effective_fingerprint: "d".repeat(64),
};
const authored = JSON.stringify({ execution: "server", action: { decision: "deny" },
  condition: { selector: { path: "output" }, evaluator: { name: "json",
    config: { field_constraints: { age: { min: { $param: "age" } } } } } },
  controls: { age: { type: "number", label: "Age", default: 18, minimum: 0, maximum: 100, integer: true } } });
const capabilities = { contract: "rule-controls-v1", fingerprint: "c".repeat(64), manifests: [{
  evaluator: "json", evaluator_version: "3.2.1", schema_fingerprint: "e".repeat(64), slots: [{
    evaluator: "json", evaluator_version: "3.2.1", schema_fingerprint: "e".repeat(64),
    path_template: "/config/field_constraints/*/min", control_types: ["number"], predicates: [],
    applicability: [], affects: "decision", expected_value_schema: { types: ["number"], minimum: 0,
      maximum: 100, integer: true }, requires_server_validation: true,
  }],
}] };
const admitted = validateAuthoredControls(authored, capabilities);
assert.equal(admitted.status, "requires_server_validation");
assert.equal(admitted.references[0]?.key, "age");
assert.equal(admitted.references[0]?.destinationPointer, "/condition/evaluator/config/field_constraints/age/min");

const operation = JSON.parse(await readFile(new URL("./receipt.json", import.meta.url), "utf8"));
operation.agent_name = "controls-agent";
operation.request.agent_name = "controls-agent";
operation.control_contract = "rule-controls-v1";
operation.agent_generation = "generation-1";
operation.decision.check.control_contract = "rule-controls-v1";
operation.decision.check.agent_generation = "generation-1";
operation.decision.check.control_resolutions = [receipt];
const rule = { id: 7, name: "age-check", rule: { execution: "server", enabled: true,
  action: { decision: "deny" }, condition: { evaluator: { name: "json",
    config: { field_constraints: { age: { min: 120 } } } } } } };
const finish = { execution_contract: "hosted-v1", control_contract: "rule-controls-v1",
  agent_generation: "generation-1", control_resolutions: [receipt], verdict: "pass", reason: "checked",
  reason_codes: [], held_by: [], would_hold_by: [], continuation: { count: 0, max: 2 },
  allow_finish: true, outcome: "completed", provider_input_tokens: null };

let badAck = null;
const calls = [];
const fetcher = async (request) => {
  calls.push(request);
  assert.equal(request.headers.get("x-norma-control-contract"), "rule-controls-v1");
  const url = new URL(request.url);
  const body = request.method === "GET" ? null : await request.clone().json();
  let payload;
  if (url.pathname.endsWith("/agents/register")) {
    payload = { created: true, overwrite_applied: false, rules: [rule], control_contract: "rule-controls-v1",
      agent_generation: "generation-1", control_resolutions: [receipt] };
  } else if (url.pathname.endsWith("/agents/controls-agent/rules")) {
    payload = { rules: [rule], control_contract: "rule-controls-v1", agent_generation: "generation-1",
      control_resolutions: [receipt] };
  } else if (url.pathname.endsWith("/checks") && body?.step?.type === "turn") {
    payload = finish;
  } else if (url.pathname.endsWith("/checks")) {
    payload = { allowed: true, confidence: 1, execution_contract: "hosted-v1",
      control_contract: "rule-controls-v1", agent_generation: "generation-1", control_resolutions: [receipt] };
  } else if (url.pathname.endsWith("/operations/f84a7edc-0d5f-4b6c-b31c-bd5cd5adbe99")) {
    payload = operation;
  } else {
    throw new Error(`Unexpected packed-consumer route ${url.pathname}`);
  }
  const responseHeaders = { "content-type": "application/json", "x-norma-control-contract": "rule-controls-v1" };
  if (badAck === "header") delete responseHeaders["x-norma-control-contract"];
  if (badAck === "envelope") payload = { ...payload, control_contract: "other" };
  return new Response(JSON.stringify(payload), { headers: responseHeaders });
};
// The public finish integration uses the configured runtime URL through the
// platform fetch boundary (unlike generated SDK calls, it does not use the
// generated HTTP transport object).
globalThis.fetch = fetcher;

const client = new NormaClient();
client.init({ agentName: "controls-agent", runtimeUrl: "https://tenant.invalid/api/runtime",
  agentKey: "synthetic-key", httpClient: new HTTPClient({ fetcher }), controlContract: "rule-controls-v1",
  observabilityEnabled: false });
const registered = await client.agents.register({ agent: { agentName: "controls-agent" } });
const listed = await client.agents.listRules({ agentName: "controls-agent" });
const checked = await client.checks.check({ stage: "pre", step: { type: "tool", name: "read", input: {} } });
const operated = await client.operations.get("f84a7edc-0d5f-4b6c-b31c-bd5cd5adbe99");
const finished = await checkFinish({ schema_version: "1", platform: "probe", platform_version: null,
  model: null, session_id: "session", turn_id: "turn", user_request: null, final_message: "done",
  actions: [], actions_truncated: false, files_touched: [], evidence_source: "sdk",
  continuation: { count: 0, max: 2 } }, { client, agentName: "controls-agent" });
assert.equal(registered.controlResolutions[0].effectiveValues.age, 120);
assert.equal(listed.controlResolutions[0].overrideRevision, 4);
assert.equal(checked.controlResolutions[0].valueSources.age, "agent_override");
assert.equal(operated.decision.controlResolutions[0].defaults.age, 90);
assert.equal(finished.control_resolutions[0].effectiveValues.age, 120);
assert.equal(calls.length, 5);
assert.ok(calls.every((request) => request.headers.get("x-norma-control-contract") === "rule-controls-v1"));
assert.ok(!calls.some((request) => new URL(request.url).pathname.endsWith("/operations/observe-metrics")));

for (const rejectedAck of ["header", "envelope"]) {
  badAck = rejectedAck;
  const before = calls.length;
  await assert.rejects(
    client.checks.check({ stage: "pre", step: { type: "tool", name: "read", input: {} } }),
    /control contract/i,
  );
  assert.equal(calls.length - before, 1, "a failed acknowledgement must never retry the check");
}
console.log("Packed Console control helper and register/read/check/operation/finish probes passed.");
