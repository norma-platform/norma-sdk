// Modified for Norma.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { NormaClient, HTTPClient } from "norma-sdk";
const artifact = JSON.parse(await readFile(new URL("./retained.json", import.meta.url), "utf8"));
assert.equal(artifact.evidenceKind, "Real local integration with scripted semantic measurements and a local spy tool");
assert.ok(Array.isArray(artifact.records) && artifact.records.length > 0);
globalThis.fetch = () => { throw new Error("Network access forbidden in receipt qualification"); };
let answers = 0;
for (const raw of artifact.records) {
  const client = new NormaClient();
  client.init({ agentName: raw.agent_name, runtimeUrl: "https://qualification.invalid", agentKey: "synthetic",
    ...(raw.target_type == null ? {} : { targetType: raw.target_type, targetId: raw.target_id }),
    httpClient: new HTTPClient({ fetcher: async () => new Response(JSON.stringify(raw), {
      headers: { "Content-Type": "application/json" },
    }) }),
  });
  const decoded = await client.operations.get(raw.id);
  assert.equal(decoded.id, raw.id);
  assert.equal(decoded.decision.outcome, raw.decision.outcome);
  assert.deepEqual(decoded.request.step.input, raw.request.step.input);
  assert.deepEqual(decoded.rules.map((item) => [item.ruleId, item.version]), raw.rules.map((item) => [item.rule_id, item.version]));
  assert.deepEqual(decoded.observations.map((item) => [item.eventKey, item.kind, item.source]), raw.observations.map((item) => [item.event_key, item.kind, item.source]));
  for (const [wire, sdk] of [["matches", "matched"], ["non_matches", "not_matched"], ["errors", "error"], ["unresolved", "unresolved"]]) {
    for (const [index, match] of (raw.decision.check[wire] ?? []).entries()) {
      const observed = match.result.semantic_observation;
      if (!observed) continue;
      const normalized = Object.fromEntries(Object.entries(observed.answers ?? {}).map(([key, answer]) => [key,
        answer.type === "statement" ? {type: "statement", support_score: answer.support_score} : answer]));
      assert.deepEqual(decoded.decision.checks.filter(item => item.status === sdk)[index].result.semanticObservation.answers, normalized);
      answers += Object.keys(observed.answers ?? {}).length;
    }
  }
}
console.log(`Retained runtime/scripted-provider receipts passed: ${artifact.records.length} records, ${answers} exact semantic answers.`);
