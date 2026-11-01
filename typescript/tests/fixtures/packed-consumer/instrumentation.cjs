const assert = require("node:assert/strict");
const { mkdtempSync, rmSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join } = require("node:path");
const sdk = require("norma-sdk");
const { CheckError, HTTPClient, configure, parseInstrumentation } = sdk;

const alpha = "packed-agent-alpha";
const bravo = "packed-agent-bravo";
assert.equal(parseInstrumentation("version: 1\nentrypoints:\n  - app.runAlpha\ncheckpoints: {}\n", "yaml").version, 1);
const directory = mkdtempSync(join(tmpdir(), "norma-packed-instrumentation-cjs-"));
const requests = [];
const connection = name => ({
  runtimeUrl: `http://localhost:8090/${name}`,
  agentKey: `key-${name}`,
  observabilityEnabled: false,
  httpClient: new HTTPClient({ fetcher: async request => {
    const url = new URL(request.url);
    const path = url.pathname.replace(`/${name}`, "");
    if (path === `/api/v1/agents/${name}`) return new Response(JSON.stringify({ agent: { agent_name: name }, steps: [] }), { headers: { "content-type": "application/json" } });
    if (path === `/api/v1/agents/${name}/rules`) return new Response(JSON.stringify({ rules: [] }), { headers: { "content-type": "application/json" } });
    if (path === "/api/v1/checks") {
      const body = JSON.parse(await request.text());
      requests.push({ name, key: request.headers.get("x-api-key"), body });
      return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
    }
    return new Response("not found", { status: 404 });
  } }),
});
(async () => {
  try {
    writeFileSync(join(directory, `${alpha}.json`), JSON.stringify({ version: 1, entrypoints: ["app.runAlpha"], checkpoints: { send: { target: "app.send" } } }));
    writeFileSync(join(directory, `${bravo}.json`), JSON.stringify({ version: 1, entrypoints: ["app.runBravo"], checkpoints: { send: { target: "app.send" } } }));
    const app = {
      send(value) { return `${value}:ok`; },
      runAlpha() { return this.send("alpha"); },
      runBravo() { return this.send("bravo"); },
    };
    const configured = await configure(directory, { bindings: { app }, connections: { [alpha]: connection(alpha), [bravo]: connection(bravo) } });
    assert.deepEqual(await Promise.all([app.runAlpha(), app.runBravo()]), ["alpha:ok", "bravo:ok"]);
    assert.deepEqual(requests.filter(item => item.body.stage === "pre").map(item => item.body.agent_name).sort(), [alpha, bravo]);
    assert.ok(requests.every(item => item.key === `key-${item.name}`));
    configured.uninstall();
    const captureApp = {
      async send(payload, options) { payload.nested.value = "mutated"; options.mode = "mutated"; return "ok"; },
      async run(...args) { return this.send(...args); },
    };
    const allParams = await configure({
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: { send: { target: "app.send", parameter_names: ["payload", "options"], input_from: "all_params" } },
    }, { agentName: alpha, bindings: { app: captureApp }, connections: { [alpha]: connection(alpha) } });
    const captureStart = requests.length;
    await captureApp.run({ nested: { value: "original" } }, { mode: "fast" }, "ignored extra");
    const allParamsSnapshots = requests.slice(captureStart).map(item => item.body.step.input);
    assert.deepEqual(allParamsSnapshots, [
      { payload: { nested: { value: "original" } }, options: { mode: "fast" } },
      { payload: { nested: { value: "original" } }, options: { mode: "fast" } },
    ]);
    allParams.uninstall();
    const guardedApp = {
      effects: 0,
      send(value) { this.effects += 1; return value; },
      run() { return this.send("guarded"); },
    };
    const schemaConfig = await configure({
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: { send: { target: "app.send", input_from: { provider: "providers.input" },
        input_schema: { type: "object", required: ["recipient"], properties: { recipient: { type: "string" } } } } },
    }, { agentName: alpha, bindings: { app: guardedApp, providers: { input: () => ({ secret: "never echo" }) } }, connections: { [alpha]: connection(alpha) } });
    const schemaStart = requests.length;
    const schemaError = await guardedApp.run().then(() => undefined, error => error);
    assert.ok(schemaError instanceof CheckError);
    assert.equal(schemaError.stage, "pre");
    assert.match(schemaError.message, /\/checkpoints\/send\/input_schema.*\/recipient/);
    assert.doesNotMatch(schemaError.message, /never echo/);
    assert.equal(schemaError.cause, undefined);
    assert.equal(requests.length, schemaStart);
    assert.equal(guardedApp.effects, 0);
    schemaConfig.uninstall();

    const providerApp = {
      effects: 0,
      send(value) { this.effects += 1; return value; },
      run() { return this.send("provider"); },
    };
    const providerConfig = await configure({ version: 2, entrypoints: ["app.run"],
      checkpoints: { send: { target: "app.send", input_from: { provider: "providers.crash" } } },
    }, { agentName: alpha, bindings: { app: providerApp, providers: { crash: () => { throw new Error("private provider payload"); } } }, connections: { [alpha]: connection(alpha) } });
    const providerStart = requests.length;
    const providerError = await providerApp.run().then(() => undefined, error => error);
    assert.ok(providerError instanceof CheckError);
    assert.match(providerError.message, /\/checkpoints\/send\/input_from.*pre/);
    assert.doesNotMatch(providerError.message, /private provider payload/);
    assert.equal(providerError.cause, undefined);
    assert.equal(requests.length, providerStart);
    assert.equal(providerApp.effects, 0);
    providerConfig.uninstall();

    const originalSend = providerApp.send;
    const slotConfig = await configure({ version: 1, entrypoints: ["app.run"], checkpoints: { send: { target: "app.send" } } },
      { agentName: alpha, bindings: { app: providerApp }, connections: { [alpha]: connection(alpha) } });
    const slotStart = requests.length;
    providerApp.send = originalSend;
    assert.throws(() => providerApp.run(), CheckError);
    assert.throws(() => slotConfig.withAgent(alpha, () => { providerApp.effects += 1; }), CheckError);
    assert.equal(providerApp.effects, 0);
    assert.equal(requests.length, slotStart);
    slotConfig.uninstall();
    const { runStreamProof } = await import("./instrumentation-stream-proof.mjs");
    await runStreamProof(sdk);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
