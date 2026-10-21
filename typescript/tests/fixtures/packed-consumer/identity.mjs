// Modified for Norma.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import norma, { NormaClient, HTTPClient, NormaAPIError, checkpoint } from "norma-sdk";

// Given: separate synthetic Agent Keys and a transport enforcing their identities.
// This checks SDK transport isolation; the server's authorization needs its own integration test.
const fixture = JSON.parse(await readFile(new URL("./receipt.json", import.meta.url), "utf8"));
const identities = new Map([
  ["synthetic-agent-alpha", "agent-alpha"],
  ["synthetic-agent-beta", "agent-beta"],
]);
const requests = [];
globalThis.fetch = () => { throw new Error("Network access is forbidden in qualification"); };
const transport = new HTTPClient({ fetcher: async (request) => {
  const body = await request.json();
  const identity = identities.get(request.headers.get("x-api-key"));
  requests.push({ identity, body });
  if (identity !== body.agent_name) {
    return new Response(JSON.stringify({ detail: "Agent Key cannot act for this agent" }), {
      status: 403, headers: { "Content-Type": "application/json" },
    });
  }
  return new Response(JSON.stringify({ ...fixture,
    operation_key: body.operation_key, agent_name: body.agent_name,
    target_type: body.target_type, target_id: body.target_id, request: body,
  }), { headers: { "Content-Type": "application/json" } });
} });
let resolutions = 0;
function options(agentName, agentKey) {
  return { agentName, agentKey, runtimeUrl: "https://qualification.invalid", httpClient: transport,
    targetType: "project", targetId: "synthetic-project", observabilityEnabled: false };
}
const alpha = new NormaClient();
const beta = new NormaClient();
alpha.init(options("agent-alpha", async () => { resolutions++; return "synthetic-agent-alpha"; }));
beta.init(options("agent-beta", "synthetic-agent-beta"));
const proposal = { operationKey: "identity-proposal", step: { type: "tool", name: "check", input: {} } };

// When: both installed clients submit a proposal concurrently.
const results = await Promise.all([alpha.operations.create(proposal), beta.operations.create(proposal)]);
// Then: clients retain their own identity, target and independently resolved credential.
assert.deepEqual(results.map((result) => result.agentName), ["agent-alpha", "agent-beta"]);
assert.equal(resolutions, 1);
assert.equal(requests.length, 2);
for (const { identity, body } of requests) {
  assert.equal(identity, body.agent_name);
  assert.equal(body.target_type, "project");
  assert.equal(body.target_id, "synthetic-project");
}

// Given: an agent name outside the provided key's server-side scope.
const mismatched = new NormaClient();
mismatched.init(options("agent-beta", "synthetic-agent-alpha"));
// When/Then: the error is preserved without a retry or invented successful decision.
await assert.rejects(mismatched.operations.create(proposal),
  (error) => error instanceof NormaAPIError && error.statusCode === 403);
assert.equal(requests.length, 3);

// Given: a guarded tool with the same rejected identity.
norma.init(options("agent-beta", "synthetic-agent-alpha"));
let effects = 0;
const guarded = checkpoint(async () => { effects++; });
// When/Then: a rejected check never executes the effect.
await assert.rejects(guarded());
assert.equal(effects, 0);
assert.equal(requests.length, 4);
console.log("Packed per-agent credential isolation and 403 fail-closed checks passed.");
