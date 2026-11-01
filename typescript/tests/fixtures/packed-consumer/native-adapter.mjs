// Modified for Norma.
import assert from "node:assert/strict";
import { Agent, Runner, Usage } from "@openai/agents";
import { HTTPClient, configure } from "norma-sdk";

const agentName = "packed-native-agent";
let modelTurn = 0;
const model = {
  async getResponse() {
    const output = modelTurn++ === 0
      ? [{ type: "function_call", callId: "packed-call", name: "send", arguments: JSON.stringify({ text: "hello" }) }]
      : [{ type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "sent" }] }];
    return { usage: new Usage(), output };
  },
};
const native = new Agent({ name: "packed-native", model, tools: [] });
const runner = new Runner({ model, tracingDisabled: true });
const app = { run: () => runner.run(native, "go") };
const effects = [];
const requests = [];
const httpClient = new HTTPClient({ fetcher: async request => {
  const url = new URL(request.url);
  const suffix = url.pathname.replace(`/${agentName}`, "");
  if (suffix === `/api/v1/agents/${agentName}`) {
    return new Response(JSON.stringify({ agent: { agent_name: agentName }, steps: [] }), { headers: { "content-type": "application/json" } });
  }
  if (suffix === `/api/v1/agents/${agentName}/rules`) {
    return new Response(JSON.stringify({ rules: [] }), { headers: { "content-type": "application/json" } });
  }
  if (suffix === "/api/v1/checks") {
    const body = await request.clone().json();
    requests.push({ body, key: request.headers.get("x-api-key") });
    return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
  }
  return new Response("not found", { status: 404 });
} });
const configured = await configure({
  version: 2,
  entrypoints: ["app.run"],
  checkpoints: {},
  adapters: { native: { kind: "openai_agents", agent: { binding: "agents.native" }, tools: {
    send: { description: "Send a message", parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false }, execute: { binding: "handlers.send" } },
  } } },
}, {
  agentName,
  bindings: { app, agents: { native }, handlers: { send: async args => { effects.push(args.text); return "sent"; } } },
  connection: { runtimeUrl: `https://tenant.invalid/${agentName}`, agentKey: "synthetic-key", httpClient, observabilityEnabled: false },
});
try {
  const result = await app.run();
  assert.equal(result.finalOutput, "sent");
  assert.deepEqual(effects, ["hello"]);
  assert.deepEqual(requests.map(item => item.body.stage), ["pre", "post"]);
  assert.ok(requests.every(item => item.body.agent_name === agentName && item.key === "synthetic-key"));
  assert.equal(native.tools.length, 1);
} finally {
  configured.uninstall();
}
assert.equal(native.tools.length, 0);
console.log("Installed ESM native Agent/Runner tool execution passed.");
