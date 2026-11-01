// Modified for Norma.
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HTTPClient } from "../src/generated/lib/http";
import { CheckError, FinishHeldError, RuleViolationError } from "../src/errors";
import { buildCheckEvents } from "../src/internal/check-events";
import { HostedOperationsApi } from "../src/internal/operations";
import { configure, parseInstrumentation, type InstrumentationConfig } from "../src/instrumentation";
import { checkpoint } from "../src/checkpoint";
import { validateCapturedInput } from "../src/instrumentation-input-schema";
import { Agent, Runner, Usage, defineToolInputGuardrail, ToolGuardrailFunctionOutputFactory, type Model, type AgentOutputItem } from "@openai/agents";

interface ConformanceFixture {
  cases: Array<{ name: string; format: "json" | "yaml"; source: string; valid: boolean; normalized?: unknown }>;
}
interface InputSchemaFixture {
  cases: Array<{
    name: string; format: "json" | "yaml"; source: string; parse_valid: boolean;
    runtime_input_check?: { input: unknown; valid: boolean; config_path?: string; instance_path?: string };
  }>;
}

const dirs: string[] = [];
const alpha = "agent-alpha";
const bravo = "agent-bravo";
interface TestRequestBody { stage?: string; agent_name?: string; step?: { name?: string; type?: string }; [key: string]: unknown }
const config = (entry: string, target: string, stepType = "tool") => JSON.stringify({
  version: 1, entrypoints: [entry], checkpoints: { send: { target, step_type: stepType } },
});
function tempDirectory(): string {
  const path = mkdtempSync(join(tmpdir(), "norma-instrumentation-")); dirs.push(path); return path;
}
afterEach(() => { for (const path of dirs.splice(0)) rmSync(path, { recursive: true, force: true }); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

function mockConnections(onCheck: (agentName: string, body: TestRequestBody) => Response | Promise<Response> = () => new Response(JSON.stringify({
  confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
}), { headers: { "content-type": "application/json" } }),
onPublish: (agentName: string, steps: unknown[]) => Response | Promise<Response> = (_agentName, steps) => new Response(JSON.stringify({ steps }), { headers: { "content-type": "application/json" } }),
onListRules: (agentName: string) => unknown = () => ({ rules: [] })) {
  const requests: Array<{ agentName: string; body: TestRequestBody }> = [];
  const httpRequests: Array<{ agentName: string; method: string; path: string; query: string; key: string | null; controlContract: string | null }> = [];
  const acknowledgeControlContract = async (request: Request, response: Response): Promise<Response> => {
    if (!response.ok || request.headers.get("X-Norma-Control-Contract") !== "rule-controls-v1") return response;
    const payload: unknown = await response.clone().json();
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return response;
    const headers = new Headers(response.headers);
    headers.set("X-Norma-Control-Contract", "rule-controls-v1");
    headers.set("content-type", "application/json");
    return new Response(JSON.stringify({ ...(payload as Record<string, unknown>), control_contract: "rule-controls-v1" }), {
      status: response.status, headers,
    });
  };
  const connection = (name: string) => ({
    runtimeUrl: `http://localhost:8080/${name}`,
    agentKey: `key-for-${name}`,
    observabilityEnabled: false,
    httpClient: new HTTPClient({ fetcher: async request => {
      const url = new URL(request.url);
      httpRequests.push({ agentName: name, method: request.method, path: url.pathname, query: url.search, key: request.headers.get("x-api-key"),
        controlContract: request.headers.get("X-Norma-Control-Contract") });
      const suffix = url.pathname.replace(`/${name}`, "");
      if (suffix === `/api/v1/agents/${name}`) return new Response(JSON.stringify({ agent: { agent_name: name }, steps: [] }), { headers: { "content-type": "application/json" } });
      if (suffix === `/api/v1/agents/${name}/rules`) {
        return acknowledgeControlContract(request, new Response(JSON.stringify(onListRules(name)), { headers: { "content-type": "application/json" } }));
      }
      if (suffix === `/api/v1/agents/${name}/steps` && request.method === "PUT") {
        const body = JSON.parse(await request.text()) as { steps: unknown[] };
        return await onPublish(name, body.steps);
      }
      if (suffix === "/api/v1/checks") {
        const body = JSON.parse(await request.text()) as TestRequestBody;
        requests.push({ agentName: name, body }); return acknowledgeControlContract(request, await onCheck(name, body));
      }
      return new Response("not found", { status: 404 });
    } }),
  });
  return { connections: { [alpha]: connection(alpha), [bravo]: connection(bravo) }, requests, httpRequests };
}

function finishResponse(verdict: "pass" | "hold" = "pass"): Response {
  const held = verdict === "hold";
  return new Response(JSON.stringify({
    execution_contract: "hosted-v1", verdict, reason: held ? "Review required" : "Complete", reason_codes: [],
    held_by: held ? [7] : [], would_hold_by: [], continuation: { count: 0, max: held ? 1 : 0 },
    allow_finish: !held, outcome: held ? "held" : "completed", provider_input_tokens: 0,
  }), { status: 200, headers: { "content-type": "application/json" } });
}

const service = () => ({
  calls: [] as string[],
  send(this: { calls: string[] }, value: string) { this.calls.push(value); return Promise.resolve(`${value}:done`); },
  runAlpha(this: { send: (value: string) => Promise<string> }, value: string) { return this.send(value); },
  runBravo(this: { send: (value: string) => Promise<string> }, value: string) { return this.send(value); },
  onlyAlpha(value: string) { return Promise.resolve(value); },
});

function runnerModel(): Model {
  let turn = 0;
  return {
    async getResponse() {
      const output: AgentOutputItem[] = turn++ === 0
        ? [{ type: "function_call", callId: "configured-call", name: "send", arguments: JSON.stringify({ text: "hello" }) }]
        : [{ type: "message", role: "assistant", status: "completed", content: [{ type: "output_text", text: "sent" }] }];
      return { usage: new Usage(), output };
    },
    async *getStreamedResponse() { yield { type: "response_done", response: { ...(await this.getResponse()), id: "configured-response" } }; },
  };
}

describe("declarative instrumentation", () => {
  it("preserves literal payload fields named path instead of treating them as selectors", () => {
    const parsed = parseInstrumentation('{"version":2,"entrypoints":["service.runAlpha"],"checkpoints":{"send":{"target":"service.send","state":{"path":["args",0]}}}}');
    expect(parsed.checkpoints.send?.state).toEqual({ path: ["args", 0] });
  });

  it.each((JSON.parse(readFileSync(new URL("../../schema/fixtures/instrumentation-conformance.json", import.meta.url), "utf8")) as ConformanceFixture).cases)(
    "matches shared parser fixture $name",
    ({ format, source, valid, normalized }) => {
      if (valid) expect(parseInstrumentation(source, format)).toEqual(normalized);
      else expect(() => parseInstrumentation(source, format)).toThrow();
    },
  );

  it.each((JSON.parse(readFileSync(new URL("../../schema/validation-fixtures/input-schema.json", import.meta.url), "utf8")) as InputSchemaFixture).cases)(
    "matches input-schema parser fixture $name",
    ({ format, source, parse_valid }) => {
      if (parse_valid) expect(() => parseInstrumentation(source, format)).not.toThrow();
      else expect(() => parseInstrumentation(source, format)).toThrow();
    },
  );

  it.each((JSON.parse(readFileSync(new URL("../../schema/validation-fixtures/input-schema.json", import.meta.url), "utf8")) as InputSchemaFixture).cases.filter(item => item.runtime_input_check))(
    "validates final captured input for shared fixture $name",
    async ({ format, source, runtime_input_check: expectation }) => {
      const parsed = parseInstrumentation(source, format);
      if (parsed.version !== 2) throw new Error("Fixture must use v2 instrumentation.");
      parsed.entrypoints = ["service.runAlpha"];
      const checkpoint = parsed.checkpoints.send;
      if (!checkpoint) throw new Error("Fixture must configure the send checkpoint.");
      checkpoint.target = "service.send";
      checkpoint.input_from = { provider: "providers.input" };
      const target = {
        effects: 0,
        async send(value: unknown) { this.effects += 1; return value; },
        async runAlpha(this: { send(value: unknown): Promise<unknown> }, value: unknown) { return this.send(value); },
      };
      const network = mockConnections();
      const configured = await configure(parsed, {
        agentName: alpha,
        bindings: { service: target, providers: { input: () => expectation!.input } },
        connections: { [alpha]: network.connections[alpha] },
      });
      const actual = await target.runAlpha("request").then(() => ({ ok: true as const }), error => ({ ok: false as const, error }));
      expect(actual.ok).toBe(expectation!.valid);
      if (!expectation!.valid) {
        expect(actual.ok ? undefined : actual.error).toBeInstanceOf(CheckError);
        if (!actual.ok) {
          expect((actual.error as Error).message).toContain(expectation!.config_path);
          expect((actual.error as Error).message).toContain(expectation!.instance_path);
        }
        expect(network.requests).toHaveLength(0);
        expect(target.effects).toBe(0);
      } else {
        expect(network.requests).toHaveLength(2);
        expect(target.effects).toBe(1);
      }
      configured.uninstall();
    },
  );

  it("parses JSON and YAML with strict schema and duplicate-key checks", () => {
    const expected = { version: 1, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send", step_type: "custom_tool" } } };
    expect(parseInstrumentation(JSON.stringify(expected))).toEqual(expected);
    expect(parseInstrumentation("version: 1\nentrypoints:\n  - service.runAlpha\ncheckpoints:\n  send:\n    target: service.send\n    step_type: custom_tool\n", "yaml")).toEqual(expected);
    expect(() => parseInstrumentation('{"version":1,"version":1,"entrypoints":["service.runAlpha"],"checkpoints":{"x":{"target":"a.b"}}}')).toThrow();
    expect(() => parseInstrumentation("version: 1\nversion: 1\nentrypoints:\n  - service.runAlpha\ncheckpoints:\n  x:\n    target: a.b\n", "yaml")).toThrow();
    expect(() => parseInstrumentation('{"version":2,"entrypoints":["service.runAlpha"],"checkpoints":{"x":{"target":"a.b","extra":true}}}')).toThrow();
    expect(() => parseInstrumentation('{"version":1,"entrypoints":["service.runAlpha"],"checkpoints":{"x":{"target":"a.b"}}} trailing')).toThrow();
  });

  it("accepts multiple v2 entrypoints with null-prototype-safe JSON value names", () => {
    const json = {
      version: 2,
      entrypoints: [
        { target: "app.nativeStream", turn: { session_id: { value: "session-a" }, turn_id: { value: "turn-a" }, finish: { stream: {}, on_hold: "raise" } } },
        { target: "app.factoryStream", returns: "async_iterable", context: { value: { valueOf: "literal", toString: "literal" } }, turn: {
          session_id: { value: "session-b" }, turn_id: { value: "turn-b" }, finish: { stream: {}, on_hold: "raise" },
        } },
      ],
      checkpoints: {},
    };
    const yaml = [
      "version: 2",
      "entrypoints:",
      "  - target: app.nativeStream",
      "    turn:",
      "      session_id: { value: session-a }",
      "      turn_id: { value: turn-a }",
      "      finish: { stream: {}, on_hold: raise }",
      "  - target: app.factoryStream",
      "    returns: async_iterable",
      "    context:",
      "      value:",
      "        valueOf: literal",
      "        toString: literal",
      "    turn:",
      "      session_id: { value: session-b }",
      "      turn_id: { value: turn-b }",
      "      finish: { stream: {}, on_hold: raise }",
      "checkpoints: {}",
    ].join("\n");

    expect(parseInstrumentation(JSON.stringify(json))).toEqual(json);
    expect(parseInstrumentation(yaml, "yaml").entrypoints).toHaveLength(2);

    const primitiveKinds = {
      version: 2,
      entrypoints: [
        { target: "app.stream", context: { value: { marker: true } } },
        { target: "app.stream", context: { value: { marker: 1 } } },
      ],
      checkpoints: {},
    };
    expect(parseInstrumentation(JSON.stringify(primitiveKinds)).entrypoints).toHaveLength(2);
  });

  it("rejects reordered structurally duplicate v2 entrypoints", () => {
    const first = { target: "app.stream", turn: { session_id: { value: "session" }, turn_id: { value: "turn" }, finish: { stream: {}, on_hold: "raise" } } };
    const reordered = { turn: { finish: { on_hold: "raise", stream: {} }, turn_id: { value: "turn" }, session_id: { value: "session" } }, target: "app.stream" };
    expect(() => parseInstrumentation(JSON.stringify({ version: 2, entrypoints: [first, reordered], checkpoints: {} })))
      .toThrow(/Invalid instrumentation configuration: .*uniqueItems/i);
  });

  it("rejects reordered duplicates with canonically equivalent but distinct Unicode keys", () => {
    const first = JSON.parse('{"target":"app.stream","context":{"value":{"\\u00e9":"one","e\\u0301":"two"}},"turn":{"session_id":{"value":"session"},"turn_id":{"value":"turn"},"finish":{"stream":{},"on_hold":"raise"}}}');
    const reordered = JSON.parse('{"turn":{"finish":{"on_hold":"raise","stream":{}},"turn_id":{"value":"turn"},"session_id":{"value":"session"}},"context":{"value":{"e\\u0301":"two","\\u00e9":"one"}},"target":"app.stream"}');
    expect(() => parseInstrumentation(JSON.stringify({ version: 2, entrypoints: [first, reordered], checkpoints: {} })))
      .toThrow(/Invalid instrumentation configuration: .*uniqueItems/i);
  });

  it("rejects native OpenAI action fields not retained by TurnRecord", () => {
    const invalid = {
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: {},
      adapters: { native: { kind: "openai_agents", agent: { binding: "framework.agent" }, tools: {
        send: { description: "Send", parameters: { type: "object" }, execute: { binding: "handlers.send" },
          action: { kind: "edit", cwd: "/tmp" } },
      } } },
    };
    expect(() => parseInstrumentation(JSON.stringify(invalid))).toThrow();
  });

  it("installs a configured OpenAI tool on the exact Agent and carries scoped request context", async () => {
    const existingGuardrail = defineToolInputGuardrail({ name: "existing-native-guardrail", run: async () => ToolGuardrailFunctionOutputFactory.allow() });
    const nativeAgent = new Agent({ name: "configured-native-agent", tools: [] });
    const runner = new Runner({ model: runnerModel(), tracingDisabled: true });
    const target = { runner, nativeAgent, async run(this: { runner: Runner; nativeAgent: Agent }) { return this.runner.run(this.nativeAgent, "go"); } };
    const checkBodies: TestRequestBody[] = [];
    const network = mockConnections((_agentName, body) => {
      checkBodies.push(body);
      return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
    });
    const executed = vi.fn(async () => "sent");
    const verdict = vi.fn();
    const config = {
      version: 2 as const,
      entrypoints: [{ target: "app.run", target_scope: { type: { value: "tenant" }, id: { value: "dynamic-tenant" } }, context: { value: { request_id: "req-1" } },
        turn: { session_id: { value: "session-1" }, turn_id: { value: "turn-1" }, finish: { final_message: { value: "sent" }, on_hold: "return", on_verdict: { binding: "handlers.verdict" } } } }],
      checkpoints: {},
      adapters: { main: { kind: "openai_agents" as const, agent: { binding: "agents.native" }, tools: {
        send: { description: "Send a message", parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false }, execute: { binding: "handlers.send" }, input_guardrails: [{ binding: "guards.input" }],
          action: { kind: "edit", target: { path: ["input", "text"] }, command: "send" } },
      } } },
    };
    const configured = await configure(config, {
      agentName: alpha,
      bindings: { app: target, agents: { native: nativeAgent }, handlers: { send: executed, verdict }, guards: { input: existingGuardrail } },
      connections: { [alpha]: { ...network.connections[alpha], targetType: "tenant", targetId: "base-tenant" } },
    });
    await target.run();
    expect(executed).toHaveBeenCalledTimes(1);
    expect(checkBodies).toHaveLength(3);
    const turn = checkBodies.find(body => body.step?.type === "turn")?.step as { input?: { actions?: Array<Record<string, unknown>> } } | undefined;
    expect(turn?.input?.actions).toMatchObject([{ kind: "edit", native_tool: "send", target: "hello", command: "send", status: "succeeded" }]);
    expect(verdict).toHaveBeenCalledTimes(1);
    expect(checkBodies.map(body => [body.target_type, body.target_id])).toEqual([
      ["tenant", "dynamic-tenant"], ["tenant", "dynamic-tenant"], ["tenant", "dynamic-tenant"],
    ]);
    expect(configured.clients[alpha]?.config?.targetId).toBe("base-tenant");
    expect(nativeAgent.tools).toHaveLength(1);
    configured.uninstall();
    expect(nativeAgent.tools).toHaveLength(0);
  });

  it("blocks a configured OpenAI tool when the handle closes during its precheck", async () => {
    const nativeAgent = new Agent({ name: "closing-native-agent", tools: [] });
    const runner = new Runner({ model: runnerModel(), tracingDisabled: true });
    const target = { runner, nativeAgent, async run(this: { runner: Runner; nativeAgent: Agent }) { return this.runner.run(this.nativeAgent, "go"); } };
    let finishCheck!: (response: Response) => void;
    const network = mockConnections(() => new Promise<Response>(resolve => { finishCheck = resolve; }));
    const execute = vi.fn(async () => "sent");
    const config = {
      version: 2 as const,
      entrypoints: ["app.run"], checkpoints: {},
      adapters: { main: { kind: "openai_agents" as const, agent: { binding: "agents.native" }, tools: {
        send: { description: "Send", parameters: { type: "object", properties: { text: { type: "string" } }, required: ["text"], additionalProperties: false }, execute: { binding: "handlers.send" } },
      } } },
    };
    const configured = await configure(config, { agentName: alpha,
      bindings: { app: target, agents: { native: nativeAgent }, handlers: { send: execute } },
      connections: { [alpha]: network.connections[alpha] },
    });
    const pending = target.run();
    await vi.waitFor(() => expect(finishCheck).toBeTypeOf("function"));
    configured.uninstall();
    finishCheck(new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    await expect(pending).rejects.toThrow();
    expect(execute).not.toHaveBeenCalled();
    expect(nativeAgent.tools).toHaveLength(0);
  });

  it("rejects params selectors on native OpenAI tools before identity reads or tool mutation", async () => {
    const nativeAgent = new Agent({ name: "params-native-agent", tools: [] });
    const target = { async run() { return "ok"; } };
    const execute = vi.fn(async () => "sent");
    const network = mockConnections();
    await expect(configure({
      version: 2,
      entrypoints: ["app.run"],
      checkpoints: {},
      adapters: { main: { kind: "openai_agents", agent: { binding: "agents.native" }, tools: {
        send: {
          description: "Send",
          parameters: { type: "object", properties: { text: { type: "string" } }, additionalProperties: false },
          execute: { binding: "handlers.send" },
          action: { kind: "edit", target: { path: ["params", "text"] } },
        },
      } } },
    }, {
      agentName: alpha,
      bindings: { app: target, agents: { native: nativeAgent }, handlers: { send: execute } },
      connections: { [alpha]: network.connections[alpha] },
    })).rejects.toThrow(/use 'input' for decoded tool arguments/);

    expect(network.httpRequests).toHaveLength(0);
    expect(nativeAgent.tools).toHaveLength(0);
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects malformed native Agent and guardrail bindings before identity reads", async () => {
    const network = mockConnections();
    const base = { version: 2 as const, entrypoints: ["app.run"], checkpoints: {}, adapters: {
      main: { kind: "openai_agents" as const, agent: { binding: "agents.native" }, tools: {
        send: { description: "Send", parameters: { type: "object", properties: {}, additionalProperties: false },
          execute: { binding: "handlers.send" }, input_guardrails: [{ binding: "guards.invalid" }] },
      } },
    } };
    const target = { async run() { return "ok"; } };
    const actual = new Agent({ name: "valid-agent", tools: [] });
    await expect(configure(base, { agentName: alpha,
      bindings: { app: target, agents: { native: actual }, handlers: { send: async () => "ok" }, guards: { invalid: {} } },
      connections: { [alpha]: network.connections[alpha] },
    })).rejects.toThrow(/native guardrail/);
    await expect(configure({ ...base, adapters: { main: { ...base.adapters.main, tools: {} } } }, { agentName: alpha,
      bindings: { app: target, agents: { native: { tools: [] } }, handlers: { send: async () => "ok" }, guards: { invalid: {} } },
      connections: { [alpha]: network.connections[alpha] },
    })).rejects.toThrow(/Agent class/);
    expect(network.httpRequests).toHaveLength(0);
  });

  it("rejects aliased native tools arrays before identity reads", async () => {
    const dir = tempDirectory();
    const sharedTools: unknown[] = [];
    const first = new Agent({ name: "first-native-agent", tools: [] });
    const second = new Agent({ name: "second-native-agent", tools: [] });
    Object.defineProperty(first, "tools", { value: sharedTools, writable: true, configurable: true });
    Object.defineProperty(second, "tools", { value: sharedTools, writable: true, configurable: true });
    const adapter = (agent: string, entrypoint: string) => ({
      version: 2,
      entrypoints: [entrypoint],
      checkpoints: {},
      adapters: { main: { kind: "openai_agents", agent: { binding: agent }, tools: {} } },
    });
    writeFileSync(join(dir, `${alpha}.json`), JSON.stringify(adapter("agents.first", "app.runAlpha")));
    writeFileSync(join(dir, `${bravo}.json`), JSON.stringify(adapter("agents.second", "app.runBravo")));
    const network = mockConnections();
    await expect(configure(dir, {
      bindings: { app: { runAlpha: async () => "ok", runBravo: async () => "ok" }, agents: { first, second } },
      connections: network.connections,
    })).rejects.toThrow(/tools array is already owned/);
    expect(network.httpRequests).toHaveLength(0);
    expect(sharedTools).toEqual([]);
  });

  it("uses explicit W3C trace IDs when building check observation events", () => {
    const events = buildCheckEvents({
      stage: "pre", agentName: alpha, traceId: "0123456789abcdef0123456789abcdef", spanId: "0123456789abcdef",
      step: { type: "tool", name: "send", input: { args: [] } },
    }, { matches: [], errors: [], nonMatches: [], unresolved: [] } as never);
    expect(events).toEqual([]);
    const observed = buildCheckEvents({
      stage: "pre", agentName: alpha, traceId: "0123456789abcdef0123456789abcdef", spanId: "0123456789abcdef",
      step: { type: "tool", name: "send", input: { args: [] } },
    }, { matches: [{ ruleId: 7, ruleName: "approval", action: "deny", result: { matched: true, confidence: 1 } }], errors: [], nonMatches: [], unresolved: [] } as never);
    expect(observed[0]).toMatchObject({ traceId: "0123456789abcdef0123456789abcdef", spanId: "0123456789abcdef" });
  });

  it("validates every agent before installation and dispatches shared targets under concurrent scopes", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), JSON.stringify({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send" }, exclusive: { target: "service.onlyAlpha" },
    } }));
    writeFileSync(join(dir, `${bravo}.yaml`), "version: 1\nentrypoints:\n  - service.runBravo\ncheckpoints:\n  send:\n    target: service.send\n");
    const target = service();
    const { connections, requests, httpRequests } = mockConnections((name) => name === alpha
      ? new Response(JSON.stringify({ confidence: 1, allowed: false, matches: [{ action: "deny", rule_id: 7, rule_name: "approval", result: { matched: true, confidence: 1 } }], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } })
      : new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    const configured = await configure(dir, { bindings: { service: target }, connections });
    const before = requests.length;
    await expect(target.send("outside")).rejects.toBeInstanceOf(CheckError);
    expect(requests).toHaveLength(before);
    const results = await Promise.all([
      expect(target.runAlpha("denied")).rejects.toBeInstanceOf(RuleViolationError),
      expect(target.runBravo("allowed")).resolves.toBe("allowed:done"),
    ]);
    void results;
    expect(target.calls).toEqual(["allowed"]);
    expect(configured.schemas[bravo]).toEqual([{ name: "send", type: "tool" }]);
    expect(requests.filter(item => item.body.stage === "pre").map(item => item.agentName).sort()).toEqual([alpha, bravo]);
    expect(requests.filter(item => item.body.stage === "pre").every(item => item.body.agent_name === item.agentName)).toBe(true);
    expect(requests.find(item => item.agentName === bravo)?.body.step.name).toBe("send");
    await expect(configured.withAgent(bravo, () => target.onlyAlpha("wrong-scope"))).rejects.toBeInstanceOf(CheckError);
    expect(target.calls).toEqual(["allowed"]);
    expect(requests.filter(item => item.body.stage === "pre")).toHaveLength(2);
    expect(httpRequests.filter(item => item.method === "POST").every(item => item.key === `key-for-${item.agentName}`)).toBe(true);
    configured.uninstall();
    configured.uninstall();
    await expect(target.send("restored")).resolves.toBe("restored:done");
  });

  it("differentiates pre-captured aliases from direct checkpoint wrappers", async () => {
    const makeTarget = () => {
      type Send = (this: { calls: string[] }, value: string) => Promise<string>;
      const target = {
        calls: [] as string[],
        async send(this: { calls: string[] }, value: string) { this.calls.push(value); return value; },
        async runAlias(this: { calls: string[] }, value: string) { return alias.call(this, value); },
      };
      let alias = target.send as Send;
      return { target, getAlias: () => alias, setAlias: (value: Send) => { alias = value; } };
    };
    const spec = { version: 1 as const, entrypoints: ["service.runAlias"], checkpoints: { send: { target: "service.send" } } };

    const declarative = makeTarget();
    const declarativeNetwork = mockConnections();
    const declarativeHandle = await configure(spec, {
      agentName: alpha,
      bindings: { service: declarative.target },
      connections: { [alpha]: declarativeNetwork.connections[alpha] },
    });
    const declarativeCheckCount = declarativeNetwork.requests.length;
    await declarative.target.runAlias("captured before configure");
    const checksThroughDeclarativeSlot = declarativeNetwork.requests.length - declarativeCheckCount;
    declarativeHandle.uninstall();

    const direct = makeTarget();
    const directNetwork = mockConnections();
    const directHandle = await configure({ ...spec, checkpoints: {} }, {
      agentName: alpha,
      bindings: { service: direct.target },
      connections: { [alpha]: directNetwork.connections[alpha] },
    });
    direct.setAlias(checkpoint(direct.getAlias(), { client: directHandle.clients[alpha], stepName: "send" }));
    const directCheckCount = directNetwork.requests.length;
    await direct.target.runAlias("captured and directly checked");
    const checksThroughDirectAlias = directNetwork.requests.length - directCheckCount;

    expect(checksThroughDeclarativeSlot).toBe(0);
    expect(checksThroughDirectAlias).toBe(2);
    expect(directNetwork.requests.slice(directCheckCount).map(item => item.body.stage)).toEqual(["pre", "post"]);
    declarativeHandle.uninstall();
    directHandle.uninstall();
  });

  it("distinguishes configured-slot dispatch from shared aliases and later reassignment", async () => {
    const target = {
      calls: [] as string[],
      async send(this: { calls: string[] }, value: string) { this.calls.push(value); return value; },
      runAlias(this: { sendAlias: (value: string) => Promise<string> }) { return this.sendAlias("shared alias"); },
      runConfigured(this: { send: (value: string) => Promise<string> }) { return this.send("configured slot"); },
      sendAlias: undefined as unknown as (value: string) => Promise<string>,
    };
    const original = target.send;
    target.sendAlias = original;
    const network = mockConnections();
    const configured = await configure({
      version: 1,
      entrypoints: ["service.runAlias", "service.runConfigured"],
      checkpoints: { send: { target: "service.send" } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });
    const afterStartup = network.requests.length;

    await target.runAlias();
    const aliasCheckCount = network.requests.length - afterStartup;
    await target.runConfigured();
    const configuredSlotCheckCount = network.requests.length - afterStartup - aliasCheckCount;
    target.send = original;
    expect(() => target.runConfigured()).toThrow(/target 'service.send' changed after installation/);

    expect(aliasCheckCount).toBe(0);
    expect(configuredSlotCheckCount).toBe(2);
    expect(network.requests.length - afterStartup - aliasCheckCount - configuredSlotCheckCount).toBe(0);
    expect(target.calls).toEqual(["shared alias", "configured slot"]);
    configured.uninstall();
  });

  it("blocks a configured entrypoint and withAgent callback when a checkpoint slot is replaced", async () => {
    const target = {
      calls: [] as string[],
      effects: 0,
      async send(this: { calls: string[] }, value: string) { this.calls.push(value); return value; },
      async runAlpha(this: { effects: number; send: (value: string) => Promise<string> }, value: string) {
        this.effects += 1;
        return this.send(value);
      },
    };
    const originalSend = target.send;
    const network = mockConnections();
    const configured = await configure({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send" } } }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });
    const afterStartupChecks = network.requests.length;
    target.send = originalSend;

    expect(() => target.runAlpha("entrypoint")).toThrow(CheckError);
    const withAgentCallback = vi.fn(() => "callback ran");
    expect(() => configured.withAgent(alpha, withAgentCallback)).toThrow(CheckError);

    expect(target.effects).toBe(0);
    expect(target.calls).toEqual([]);
    expect(withAgentCallback).not.toHaveBeenCalled();
    expect(network.requests).toHaveLength(afterStartupChecks);
    configured.uninstall();
  });

  it("rechecks slot ownership after an asynchronous context provider before the app callback", async () => {
    let resolveContext!: (value: { tenant: string }) => void;
    const target = {
      effects: 0,
      async send(value: string) { return value; },
      async runAlpha(this: { effects: number; send: (value: string) => Promise<string> }, value: string) {
        this.effects += 1;
        return this.send(value);
      },
    };
    const originalSend = target.send;
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: [{ target: "service.runAlpha", context: { provider: "providers.context" } }],
      checkpoints: { send: { target: "service.send" } },
    }, {
      agentName: alpha,
      bindings: { service: target, providers: { context: () => new Promise<{ tenant: string }>(resolve => { resolveContext = resolve; }) } },
      connections: { [alpha]: network.connections[alpha] },
    });
    const pending = target.runAlpha("request");
    target.send = originalSend;
    resolveContext({ tenant: "acme" });

    await expect(pending).rejects.toBeInstanceOf(CheckError);
    expect(target.effects).toBe(0);
    expect(network.requests).toHaveLength(0);
    configured.uninstall();
  });

  it("allows a literal payload mapping that omits a hosted Rule selector path", async () => {
    const target = service();
    const originalSend = target.send;
    const hostedRule = {
      id: 7,
      name: "recipient-required",
      rule: { execution: "server", enabled: true, action: { decision: "deny" },
        condition: { selector: { path: "input.to" }, evaluator: { name: "json", config: {} } } },
    };
    const network = mockConnections(undefined, undefined, () => ({ rules: [hostedRule] }));
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: { value: { subject: "Hello" } } } },
      schemas: { publish: true },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });

    await expect(target.runAlpha("request")).resolves.toBe("request:done");

    expect(network.requests.filter(item => item.body.stage === "pre").at(-1)?.body.step?.input).toEqual({ subject: "Hello" });
    expect(network.requests.some(item => item.body.step?.input && typeof item.body.step.input === "object" && "to" in item.body.step.input)).toBe(false);
    expect(target.send).not.toBe(originalSend);
    configured.uninstall();
  });

  it("rejects an invalid literal input mapping against its local input_schema before network access", async () => {
    const target = service();
    const originalSend = target.send;
    const network = mockConnections();
    const error = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: {
        target: "service.send",
        input_from: { value: { subject: "Hello" } },
        input_schema: { type: "object", properties: { subject: { type: "number" } } },
      } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    }).then(() => undefined, cause => cause);
    expect(error).toBeInstanceOf(TypeError);
    expect((error as Error).message).toContain("/checkpoints/send/input_from/value");
    expect((error as Error).message).toContain("/subject");
    expect(target.send).toBe(originalSend);
    expect(network.httpRequests).toHaveLength(0);
  });

  it("validates the final captured input before checks and effects, without echoing invalid data", async () => {
    const target = {
      effects: 0,
      async send(value: { to?: string }) { this.effects += 1; return value; },
      async runAlpha(this: { send(value: { to?: string }): Promise<unknown> }, value: { to?: string }) { return this.send(value); },
    };
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: {
        target: "service.send",
        parameter_names: ["value"],
        input_from: "all_params",
        input_schema: { type: "object", required: ["value"], properties: { value: { type: "object", required: ["to"], properties: { to: { type: "string" } } } } },
      } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha({ private: "DO NOT ECHO" }).then(() => undefined, cause => cause);
    expect(error).toBeInstanceOf(CheckError);
    expect((error as Error).message).toContain("/checkpoints/send/input_schema");
    expect((error as Error).message).toContain("/value");
    expect((error as Error).message).not.toContain("DO NOT ECHO");
    expect((error as Error & { cause?: unknown }).cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.effects).toBe(0);
    configured.uninstall();
  });

  it("validates the SDK default captured input envelope when input_from is omitted", async () => {
    const target = {
      effects: 0,
      async send(value: string) { this.effects += 1; return value; },
      async runAlpha(this: { send(value: string): Promise<string> }, value: string) { return this.send(value); },
    };
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: {
        type: "object", required: ["args"], properties: { args: { type: "array", minItems: 1, items: { type: "string" } } },
      } } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });
    await expect(target.runAlpha("request")).resolves.toBe("request");
    expect(network.requests[0]?.body.step?.input).toEqual({ args: ["request"] });
    expect(target.effects).toBe(1);
    configured.uninstall();
  });

  it("fails closed if an internal input schema validator is unexpectedly absent", () => {
    expect(() => validateCapturedInput({}, { private: "DO NOT ECHO" }, "send")).toThrow(/input_schema.*unavailable/);
  });

  it.each([
    { field: "context_from", value: "not an object" },
    { field: "tools_from", value: [1] },
  ])("reports safe source failures for invalid $field values", async ({ field, value }) => {
    const target = {
      effects: 0,
      async send(value: string) { this.effects += 1; return value; },
      async runAlpha(this: { send(value: string): Promise<string> }, value: string) { return this.send(value); },
    };
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", [field]: { value } } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);
    expect(error).toBeInstanceOf(CheckError);
    expect((error as Error).message).toContain(`/checkpoints/send/${field}`);
    expect((error as Error).message).not.toContain("not an object");
    expect((error as Error & { cause?: unknown }).cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.effects).toBe(0);
    configured.uninstall();
  });

  it("accepts optional and null inputs without coercion or inserted defaults", async () => {
    const target = service();
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: {
        target: "service.send",
        input_from: { value: null },
        input_schema: { type: ["object", "null"], properties: { note: { type: "string", default: "not inserted" } }, additionalProperties: false },
      } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });
    await expect(target.runAlpha("request")).resolves.toBe("request:done");
    expect(network.requests[0]?.body.step?.input).toBeNull();
    configured.uninstall();
  });

  it("rejects unsupported input_schema dialects and reference forms during parsing", () => {
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { $schema: "https://json-schema.org/draft/2020-12/schema" } } },
    }))).toThrow(/\/checkpoints\/send\/input_schema/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { $ref: "https://example.com/schema.json" } } },
    }))).toThrow(/\/checkpoints\/send\/input_schema/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { $ref: "#/definitions/node", definitions: { node: { $ref: "#/definitions/node" } } } } },
    }))).toThrow(/cyclic local \$ref graph/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: { child: { $ref: "#" } } } } },
    }))).toThrow(/cyclic local \$ref graph/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: { email: { type: "string", format: "email" } } } } },
    }))).toThrow(/format is not supported/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", unevaluatedProperties: false } } },
    }))).toThrow(/unsupported keyword/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", const: { "$ref": "not-a-reference" } } } },
    }))).not.toThrow();
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: { value: "accepted" }, input_schema: {
        $ref: "#/definitions/text", type: "null", definitions: { text: { type: "string" } },
      } } },
    }))).not.toThrow();
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: { value: { $schema: "https://json-schema.org/draft/2020-12/schema" } } } } },
    }))).toThrow(/only Draft 7/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: {
        value: { $schema: "http://json-schema.org/draft-07/schema#", type: "string" },
      } } } },
    }))).not.toThrow();
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { $ref: "#/const/value", const: { value: { type: "string" } } } } },
    }))).toThrow(/not a schema node/);
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: { "$ref": { type: "string" } } } } },
    }))).not.toThrow();
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { type: "object", properties: { value: { $ref: "#/definitions/value" } }, definitions: { value: { type: "string" } } } } },
    }))).not.toThrow();
  });

  it("rejects local-reference DAGs whose expanded Draft 7 graph exceeds the compile bound", () => {
    const definitions: Record<string, unknown> = { leaf: { type: "string" } };
    for (let level = 0; level < 13; level += 1) {
      const prior = level === 0 ? "leaf" : `node${level - 1}`;
      definitions[`node${level}`] = { allOf: [{ $ref: `#/definitions/${prior}` }, { $ref: `#/definitions/${prior}` }] };
    }
    expect(() => parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_schema: { $ref: "#/definitions/node12", definitions } } },
    }))).toThrow(/schema graph exceeds the supported depth or size/);
  });

  it("fails provider and credential binding resolution before HTTP or target mutation", async () => {
    const cases = [
      {
        name: "state provider",
        config: { version: 2, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send", state_from: { provider: "providers.capture" } } } },
        bindings: { service: service() },
        expected: /\/checkpoints\/send\/state_from.*ConfigureOptions\.bindings.*providers\.capture/,
      },
      {
        name: "agent key",
        config: { version: 2, entrypoints: ["service.runAlpha"], checkpoints: {}, connection: { agent_key: { binding: "keys.current" } } },
        bindings: { service: service() },
        expected: /\/connection\/agent_key.*ConfigureOptions\.bindings.*keys\.current/,
      },
      {
        name: "state_provider registry",
        config: { version: 2, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send", state_provider: "capture" } } },
        bindings: { service: service() },
        expected: /\/checkpoints\/send\/state_provider.*ConfigureOptions\.providers.*capture/,
      },
    ] as const;

    for (const item of cases) {
      const target = item.bindings.service;
      const originalSend = target.send;
      const network = mockConnections();
      await expect(configure(item.config, {
        agentName: alpha,
        bindings: item.bindings,
        connections: { [alpha]: network.connections[alpha] },
      })).rejects.toThrow(item.expected);
      expect(target.send, item.name).toBe(originalSend);
      expect(network.httpRequests, item.name).toHaveLength(0);
    }
  });

  it("reports a source-aware safe CheckError when an input provider fails", async () => {
    const target = service();
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: { provider: "providers.capture" } } },
    }, {
      agentName: alpha,
      bindings: { service: target, providers: { capture: () => { throw new Error("PRIVATE INPUT PAYLOAD"); } } },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);

    expect(error).toBeInstanceOf(CheckError);
    expect(error).toMatchObject({ stage: "pre" });
    expect(error.message).toContain("/checkpoints/send/input_from");
    expect(error.message).toContain("send");
    expect(error.message).not.toContain("PRIVATE INPUT PAYLOAD");
    expect(error.cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.calls).toEqual([]);
    configured.uninstall();
  });

  it.each(["target", "command", "cwd"] as const)("reports safe source-aware CheckErrors for declarative action %s failures", async field => {
    const target = {
      effects: 0,
      async send(value: string) { this.effects += 1; return value; },
      async runAlpha(this: { send(value: string): Promise<string> }, value: string) { return this.send(value); },
    };
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", action: { kind: "edit", [field]: { provider: "providers.target" } } } },
    }, {
      agentName: alpha,
      bindings: { service: target, providers: { target: () => { throw new Error("PRIVATE ACTION TARGET"); } } },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);
    expect(error).toBeInstanceOf(CheckError);
    expect((error as Error).message).toContain(`/checkpoints/send/action/${field}`);
    expect((error as Error).message).toContain("pre");
    expect((error as Error).message).not.toContain("PRIVATE ACTION TARGET");
    expect((error as Error & { cause?: unknown }).cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.effects).toBe(0);
    configured.uninstall();
  });

  it("reports a safe pointer CheckError when an action ValueSource resolves to the wrong type", async () => {
    const target = {
      effects: 0,
      async send(value: string) { this.effects += 1; return value; },
      async runAlpha(this: { send(value: string): Promise<string> }, value: string) { return this.send(value); },
    };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", action: { kind: "edit", target: { provider: "providers.target" } } } },
    }, { agentName: alpha, bindings: { service: target, providers: { target: () => ({ private: "result" }) } }, connections: { [alpha]: network.connections[alpha] } });
    const error = await target.runAlpha("request").then(() => undefined, cause => cause);
    expect(error).toBeInstanceOf(CheckError);
    expect((error as Error).message).toContain("/checkpoints/send/action/target");
    expect((error as Error).message).not.toContain("private");
    expect((error as Error & { cause?: unknown }).cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.effects).toBe(0);
    configured.uninstall();
  });

  it("reports a source-aware CheckError when a state_from provider fails", async () => {
    const target = service();
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", state_from: { provider: "providers.capture" } } },
    }, {
      agentName: alpha,
      bindings: { service: target, providers: { capture: () => { throw new Error("PRIVATE STATE PAYLOAD"); } } },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);

    expect(error).toBeInstanceOf(CheckError);
    expect(error).toMatchObject({ stage: "pre" });
    expect(error.message).toContain("/checkpoints/send/state_from");
    expect(error.message).not.toContain("PRIVATE STATE PAYLOAD");
    expect(error.cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.calls).toEqual([]);
    configured.uninstall();
  });

  it("reports a source-aware post CheckError without retaining output provider exceptions", async () => {
    const target = service();
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", output_from: { provider: "providers.capture" } } },
    }, {
      agentName: alpha,
      bindings: { service: target, providers: { capture: () => { throw new Error("PRIVATE OUTPUT PAYLOAD"); } } },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);

    expect(error).toBeInstanceOf(CheckError);
    expect(error).toMatchObject({ stage: "post" });
    expect(error.message).toContain("/checkpoints/send/output_from");
    expect(error.message).toContain("send");
    expect(error.message).not.toContain("PRIVATE OUTPUT PAYLOAD");
    expect(error.cause).toBeUndefined();
    expect(target.calls).toEqual(["request"]);
    configured.uninstall();
  });

  it("reports a source-aware CheckError when a configured input path is absent", async () => {
    const target = service();
    const network = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: {
        target: "service.send",
        parameter_names: ["request"],
        input_from: { path: ["params", "request", "missing"] },
      } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    });

    const error = await target.runAlpha("request").then(() => undefined, cause => cause);

    expect(error).toBeInstanceOf(CheckError);
    expect(error).toMatchObject({ stage: "pre" });
    expect(error.message).toContain("/checkpoints/send/input_from");
    expect(error.cause).toBeUndefined();
    expect(network.requests).toHaveLength(0);
    expect(target.calls).toEqual([]);
    configured.uninstall();
  });

  it("validates only enabled Rules in the owning target scope during startup", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), config("service.runAlpha", "service.send"));
    const target = service();
    const transport = mockConnections();
    const connection = {
      ...(transport.connections[alpha] as object),
      targetType: "repository",
      targetId: "repo-7",
    };
    const configured = await configure(dir, {
      bindings: { service: target },
      connections: { [alpha]: connection },
    });
    const rulesRequest = transport.httpRequests.find(item => item.path.endsWith(`/agents/${alpha}/rules`));
    expect(rulesRequest?.method).toBe("GET");
    const query = new URLSearchParams(rulesRequest?.query);
    expect(query.get("enabled_state")).toBe("enabled");
    expect(query.get("target_type")).toBe("repository");
    expect(query.get("target_id")).toBe("repo-7");
    configured.uninstall();
  });

  it("resolves v2 connection env and binding values, then applies API connection precedence", async () => {
    const previousUrl = process.env.NORMA_V2_RUNTIME_URL;
    const previousKey = process.env.NORMA_V2_AGENT_KEY;
    const previousMissingKey = process.env.NORMA_V2_MISSING_KEY;
    process.env.NORMA_V2_RUNTIME_URL = "http://localhost:8080/agent-alpha";
    process.env.NORMA_V2_AGENT_KEY = "key-from-v2-env";
    const target = service();
    const transport = mockConnections();
    const config: InstrumentationConfig = {
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send" } },
      connection: {
        runtime_url: { env: "NORMA_V2_RUNTIME_URL" },
        agent_key: { env: "NORMA_V2_AGENT_KEY" },
        http_client: { binding: "transports.http" },
        timeout_ms: 500,
        target_type: "repository",
        target_id: "file-repository",
      },
      telemetry: { enabled: false },
    };
    try {
      const bindings = { service: target, transports: { http: transport.connections[alpha]!.httpClient } };
      const configured = await configure(config, { agentName: alpha, bindings });
      expect(configured.clients[alpha]?.config).toMatchObject({
        runtimeUrl: "http://localhost:8080/agent-alpha", timeoutMs: 500,
        targetType: "repository", targetId: "file-repository", observabilityEnabled: false,
      });
      await target.runAlpha("from-file");
      expect(transport.httpRequests.find(item => item.path.endsWith(`/agents/${alpha}`))?.key).toBe("key-from-v2-env");
      expect(transport.requests.at(-1)?.body).toMatchObject({ target_type: "repository", target_id: "file-repository" });
      configured.uninstall();

      const overridden = await configure(config, {
        agentName: alpha,
        bindings,
        connection: { runtimeUrl: "http://localhost:8080/agent-alpha", agentKey: "shared-key", targetType: "organization", targetId: "shared-target", timeoutMs: 700 },
        connections: { [alpha]: { agentKey: "agent-key", targetType: "repository", targetId: "per-agent-target", timeoutMs: 900 } },
      });
      expect(overridden.clients[alpha]?.config).toMatchObject({
        runtimeUrl: "http://localhost:8080/agent-alpha", timeoutMs: 900,
        targetType: "repository", targetId: "per-agent-target", observabilityEnabled: false,
      });
      await target.runAlpha("from-api");
      const lastRequest = transport.httpRequests.filter(item => item.method === "POST" && item.path.endsWith("/checks")).at(-1);
      expect(lastRequest?.key).toBe("agent-key");
      expect(transport.requests.at(-1)?.body).toMatchObject({ target_type: "repository", target_id: "per-agent-target" });
      expect(transport.httpRequests.some(item => item.path.endsWith("/observability/events"))).toBe(false);
      overridden.uninstall();

      delete process.env.NORMA_V2_MISSING_KEY;
      const missingFileKey = await configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {},
        connection: { agent_key: { env: "NORMA_V2_MISSING_KEY" } } }, {
        agentName: alpha, bindings: { service: target },
        connection: { runtimeUrl: "http://localhost:8080/agent-alpha", agentKey: "explicit-shared-key" },
        connections: { [alpha]: { httpClient: transport.connections[alpha]!.httpClient } },
      });
      expect(missingFileKey.clients[alpha]?.config?.agentName).toBe(alpha);
      missingFileKey.uninstall();
    } finally {
      if (previousUrl === undefined) delete process.env.NORMA_V2_RUNTIME_URL; else process.env.NORMA_V2_RUNTIME_URL = previousUrl;
      if (previousKey === undefined) delete process.env.NORMA_V2_AGENT_KEY; else process.env.NORMA_V2_AGENT_KEY = previousKey;
      if (previousMissingKey === undefined) delete process.env.NORMA_V2_MISSING_KEY; else process.env.NORMA_V2_MISSING_KEY = previousMissingKey;
    }
  });

  it("publishes explicit and derived schemas through the owning authenticated transport", async () => {
    const target = service(); const published: unknown[][] = [];
    const network = mockConnections(undefined, (_agentName, steps) => {
      published.push(steps);
      return new Response(JSON.stringify({ steps }), { headers: { "content-type": "application/json" } });
    });
    const configured = await configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send" }, extra: { target: "service.onlyAlpha" },
    }, schemas: { publish: true, steps: [{ type: "tool", name: "send", input_schema: {
      type: "object", properties: { input_schema: { type: "string" } },
    } }] } }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    expect(published).toEqual([[{
      type: "tool", name: "send", input_schema: { type: "object", properties: { input_schema: { type: "string" } } },
    }, { type: "tool", name: "extra" }]]);
    const put = network.httpRequests.find(item => item.method === "PUT");
    expect(put).toMatchObject({ path: `/${alpha}/api/v1/agents/${alpha}/steps`, key: `key-for-${alpha}` });
    expect(configured.schemas[alpha]).toEqual([
      { type: "tool", name: "send", inputSchema: { type: "object", properties: { input_schema: { type: "string" } } } },
      { type: "tool", name: "extra" },
    ]);
    configured.uninstall();
  });

  it("leaves target slots untouched when explicit schema publication fails", async () => {
    const target = service(); const original = target.runAlpha;
    const network = mockConnections(undefined, () => new Response(JSON.stringify({ error_code: "SCHEMA_INCOMPATIBLE" }), { status: 409, headers: { "content-type": "application/json" } }));
    await expect(configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {},
      schemas: { publish: true, steps: [{ type: "tool", name: "new-step" }] } }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] },
    })).rejects.toMatchObject({ statusCode: 409 });
    expect(target.runAlpha).toBe(original);
  });

  it("maps v2 checkpoint values into the existing pre/post check payload", async () => {
    const target = service();
    const transport = mockConnections();
    const candidate: InstrumentationConfig = {
      version: 2,
      entrypoints: [{
        target: "service.runAlpha",
        context: { value: { tenant: "acme" } },
        target_scope: { type: { value: "repository" }, id: { path: ["args", 0] } },
      }],
      checkpoints: {
        send: {
          target: "service.send",
          action: { kind: "shell", command: { path: ["args", 0] } },
          state_from: { provider: "providers.capture" },
          input_from: { path: ["args", 0] },
          output_from: { path: ["result"] },
          context_from: { path: ["context"] },
          tools_from: { value: [{ name: "search" }] },
          ground_truth_from: { value: { expected: "done" } },
        },
      },
    };
    const configured = await configure(candidate, {
      agentName: alpha,
      bindings: { service: target, providers: { capture: ({ args }: { args: unknown[] }) => ({ input: args[0] }) } },
      connections: { [alpha]: transport.connections[alpha] },
    });
    await expect(target.runAlpha("mapped")).resolves.toBe("mapped:done");
    expect(transport.requests.filter(item => item.body.stage === "pre").at(-1)?.body).toMatchObject({
      target_type: "repository", target_id: "mapped",
      step: {
      input: "mapped",
      context: { tenant: "acme", state: { input: "mapped" }, norma_action: { kind: "shell", command: "mapped" } },
      tools: [{ name: "search" }],
      ground_truth: { expected: "done" },
      },
    });
    expect(transport.requests.filter(item => item.body.stage === "post").at(-1)?.body.step).toMatchObject({
      input: "mapped", output: "mapped:done", context: { tenant: "acme", norma_action: { kind: "shell", command: "mapped" } },
    });
    configured.uninstall();
  });

  it("selects fields from the first argument through the input root", async () => {
    const target = service();
    const transport = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: {
        send: { target: "service.send", input_from: { path: ["input", "message"] } },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha({ message: "selected from input" } as unknown as string);

    expect(transport.requests.find(item => item.body.stage === "pre")?.body.step?.input).toBe("selected from input");
    configured.uninstall();
  });

  it("resolves named checkpoint parameters and preserves the target method receiver", async () => {
    const target = {
      marker: "service receiver",
      async send(this: { marker: string }, request: { message: string }) {
        return `${this.marker}: ${request.message}`;
      },
      async run(this: { send: (request: { message: string }) => Promise<string> }, request: { message: string }) {
        return this.send(request);
      },
    };
    const transport = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.run"],
      checkpoints: {
        send: {
          target: "service.send",
          parameter_names: ["$request"],
          input_from: { path: ["params", "$request", "message"] },
          options: { action: {
            kind: "edit",
            target: { path: ["params", "$request", "message"] },
            command: { path: ["input", "message"] },
          } },
        },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await expect(target.run({ message: "mapped argument" })).resolves.toBe("service receiver: mapped argument");

    expect(transport.requests.find(item => item.body.stage === "pre")?.body.step?.input).toBe("mapped argument");
    expect(transport.requests.find(item => item.body.stage === "pre")?.body.step?.context).toMatchObject({
      norma_action: { kind: "edit", target: "mapped argument", command: "mapped argument" },
    });
    configured.uninstall();
  });

  it("rejects missing parameter metadata and unknown parameter selectors before HTTP or mutation", async () => {
    const target = service();
    const originalSend = target.send;
    const withoutMetadata = mockConnections();
    await expect(configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: { path: ["params", "request", "message"] } } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: withoutMetadata.connections[alpha] },
    })).rejects.toThrow(/requires parameter_names/);

    const unknownParameter = mockConnections();
    await expect(configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: {
        send: { target: "service.send", parameter_names: ["request"], input_from: { path: ["params", "missing"] } },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: unknownParameter.connections[alpha] },
    })).rejects.toThrow(/undeclared parameter/);

    expect(withoutMetadata.httpRequests).toHaveLength(0);
    expect(unknownParameter.httpRequests).toHaveLength(0);
    expect(target.send).toBe(originalSend);
  });

  it("resolves context_from through named checkpoint arguments and validates its object shape", async () => {
    const runCase = async (contextFrom: InstrumentationConfig["checkpoints"][string]["context_from"]) => {
      const calls: unknown[][] = [];
      const checkpoints = {
        async dummyTool(a: unknown, b: unknown) { calls.push([a, b]); return "done"; },
      };
      const app = {
        runDummy(a: unknown, b: unknown) {
          // Exercise the configured module property so the checkpoint dispatcher runs.
          return checkpoints.dummyTool(a, b);
        },
      };
      const transport = mockConnections();
      const configured = await configure({
        version: 2,
        entrypoints: ["app.runDummy"],
        checkpoints: { dummyTool: {
          target: "checkpoints.dummyTool",
          parameter_names: ["a", "b"],
          context_from: contextFrom!,
        } },
      }, {
        agentName: alpha,
        bindings: { app, checkpoints },
        connections: { [alpha]: transport.connections[alpha] },
      });
      return { app, calls, transport, configured };
    };

    const objectArgument = { note: "from named argument" };
    const pathCase = await runCase({ path: ["params", "a"] });
    await expect(pathCase.app.runDummy(objectArgument, "y")).resolves.toBe("done");
    expect(pathCase.calls).toEqual([[objectArgument, "y"]]);
    const pathContext = pathCase.transport.requests.find(item => item.body.stage === "pre")?.body.step as unknown as { context?: Record<string, unknown> };
    expect(pathContext.context).toMatchObject({ note: "from named argument" });
    pathCase.configured.uninstall();

    const scalarCase = await runCase({ path: ["params", "b"] });
    await expect(scalarCase.app.runDummy(objectArgument, "y")).rejects.toThrow(/context_from.*must resolve to a JSON object/);
    expect(scalarCase.calls).toEqual([]);
    expect(scalarCase.transport.requests).toEqual([]);
    scalarCase.configured.uninstall();

    const literalCase = await runCase({ value: { note: "static context" } });
    await expect(literalCase.app.runDummy(objectArgument, "y")).resolves.toBe("done");
    expect(literalCase.calls).toEqual([[objectArgument, "y"]]);
    const literalContext = literalCase.transport.requests.find(item => item.body.stage === "pre")?.body.step as unknown as { context?: Record<string, unknown> };
    expect(literalContext.context).toMatchObject({ note: "static context" });
    literalCase.configured.uninstall();
  });

  it("rejects Python-only all_kwargs input mapping before HTTP or application execution", async () => {
    const target = service();
    const originalSend = target.send;
    const transport = mockConnections();
    await expect(configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: "all_kwargs" } },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    })).rejects.toMatchObject({
      code: "capability",
      path: "/checkpoints/send/input_from",
    });
    expect(transport.httpRequests).toHaveLength(0);
    expect(target.send).toBe(originalSend);
    expect(target.calls).toEqual([]);
  });

  it("rejects all_params without parameter_names before HTTP or target mutation", async () => {
    const target = service();
    const originalSend = target.send;
    const transport = mockConnections();
    await expect(configure(parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send", input_from: "all_params" } },
    })), {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    })).rejects.toMatchObject({ code: "selector", path: "/checkpoints/send/input_from" });
    expect(transport.httpRequests).toHaveLength(0);
    expect(target.send).toBe(originalSend);
    expect(target.calls).toEqual([]);
  });

  it("keeps prototype-named parameters as ordinary data selectors", async () => {
    const target = service();
    const transport = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: {
        send: {
          target: "service.send",
          parameter_names: ["__proto__"],
          input_from: { path: ["params", "__proto__", "message"] },
        },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha({ message: "ordinary data" } as unknown as string);

    expect(transport.requests.find(item => item.body.stage === "pre")?.body.step?.input).toBe("ordinary data");
    configured.uninstall();
  });

  it("keeps omitted positional parameters missing instead of using function defaults", async () => {
    const target = {
      calls: [] as string[],
      async send(this: { calls: string[] }, request: { value: string }, options = "function default") {
        this.calls.push(options);
        return request.value;
      },
      async run(this: { send: (request: { value: string }) => Promise<string> }, request: { value: string }) {
        return this.send(request);
      },
    };
    const transport = mockConnections();
    const configured = await configure({
      version: 2,
      entrypoints: ["service.run"],
      checkpoints: {
        send: {
          target: "service.send",
          parameter_names: ["request", "options"],
          input_from: { path: ["params", "options"] },
        },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });
    const startupRequestCount = transport.httpRequests.length;

    await expect(target.run({ value: "submitted" })).rejects.toThrow(/path is missing/);

    expect(target.calls).toEqual([]);
    expect(transport.httpRequests).toHaveLength(startupRequestCount);
    configured.uninstall();
  });

  it("captures named checkpoint arguments as one object and excludes defaults and extra arguments", async () => {
    const target = {
      calls: [] as unknown[][],
      async send(this: { calls: unknown[][] }, request: { id: string }, options = { mode: "default" }) {
        request.id = "mutated";
        options.mode = "mutated";
        this.calls.push([request, options]);
        return request.id;
      },
      async run(this: { send: (...args: unknown[]) => Promise<unknown> }, ...args: unknown[]) {
        return this.send(...args);
      },
    };
    const transport = mockConnections();
    const candidate = JSON.stringify({
      version: 2,
      entrypoints: ["service.run"],
      checkpoints: { send: { target: "service.send", parameter_names: ["request", "options"], input_from: "all_params" } },
    });
    const configured = await configure(parseInstrumentation(candidate), {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });
    await target.run({ id: "first" }, { mode: "provided" }, "unnamed extra");
    await target.run({ id: "second" });

    const firstSnapshot = { request: { id: "first" }, options: { mode: "provided" } };
    const secondSnapshot = { request: { id: "second" } };
    expect(transport.requests.map(item => item.body.step?.input)).toEqual([
      firstSnapshot,
      firstSnapshot,
      secondSnapshot,
      secondSnapshot,
    ]);
    expect(target.calls).toEqual([
      [{ id: "mutated" }, { mode: "mutated" }],
      [{ id: "mutated" }, { mode: "mutated" }],
    ]);
    await expect(target.run({ id: "invalid" }, undefined)).rejects.toThrow(/Checkpoint arguments must contain only finite JSON values/);
    expect(target.calls).toHaveLength(2);
    configured.uninstall();
  });

  it("preserves prototype-named keys in all_params captures and accepts explicit empty names", async () => {
    const target = {
      async send(...args: unknown[]) { return args[0]; },
      async run(this: { send: (...args: unknown[]) => Promise<unknown> }, ...args: unknown[]) { return this.send(...args); },
    };
    const transport = mockConnections();
    const configured = await configure(parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.run"],
      checkpoints: { send: { target: "service.send", parameter_names: ["__proto__", "constructor"], input_from: "all_params" } },
    })), {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });
    await target.run("proto value", "constructor value");

    const input = transport.requests.find(item => item.body.stage === "pre")?.body.step?.input as Record<string, unknown>;
    expect(Object.keys(input)).toEqual(["__proto__", "constructor"]);
    expect(Object.hasOwn(input, "__proto__")).toBe(true);
    expect(input["__proto__"]).toBe("proto value");
    expect(input.constructor).toBe("constructor value");
    configured.uninstall();

    const emptyTransport = mockConnections();
    const emptyNames = await configure(parseInstrumentation(JSON.stringify({
      version: 2,
      entrypoints: ["service.run"],
      checkpoints: { send: { target: "service.send", parameter_names: [], input_from: "all_params" } },
    })), {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: emptyTransport.connections[alpha] },
    });
    await target.run("unnamed extra");
    expect(emptyTransport.requests.find(item => item.body.stage === "pre")?.body.step?.input).toEqual({});
    emptyNames.uninstall();
  });

  it("maps application aliases and named entrypoint selectors onto the existing finish wire", async () => {
    const target = service();
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishResponse() : finishResponse());
    const configured = await configure({
      version: 2,
      entrypoints: [{
        target: "service.runAlpha",
        returns: "promise",
        parameter_names: ["request"],
        turn: {
          session_id: { path: ["params", "request", "session_id"] },
          turn_id: { path: ["input", "turn_id"] },
          application: "customer-app",
          application_version: "2.4.1",
          finish: { final_message: { path: ["result"] }, on_hold: "raise" },
        },
      }],
      checkpoints: {},
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha({ session_id: "session-1", turn_id: "turn-1" } as unknown as string);

    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({
      session_id: "session-1",
      turn_id: "turn-1",
      platform: "customer-app",
      platform_version: "2.4.1",
      final_message: "[object Object]:done",
    });
    configured.uninstall();
  });

  it("rejects conflicting application aliases before HTTP or target mutation", async () => {
    const target = service();
    const original = target.runAlpha;
    const network = mockConnections();

    await expect(configure({
      version: 2,
      entrypoints: [{ target: "service.runAlpha", turn: {
        session_id: { value: "session" }, turn_id: { value: "turn" },
        application: "new-name", platform: "old-name",
      } }],
      checkpoints: {},
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: network.connections[alpha] },
    })).rejects.toThrow(/turn\.application\/platform values must match/);

    expect(network.httpRequests).toHaveLength(0);
    expect(target.runAlpha).toBe(original);
  });

  it.each(["file config", "ConfigureOptions connection"])("normalizes rule-controls from %s to the strict versioned wire token", async source => {
    const target = service();
    const transport = mockConnections();
    const config: InstrumentationConfig = {
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send" } },
      ...(source === "file config" ? { connection: { control_contract: "rule-controls" as const } } : {}),
    };
    const connection = {
      ...transport.connections[alpha],
      ...(source === "ConfigureOptions connection" ? { controlContract: "rule-controls" as const } : {}),
    };
    const configured = await configure(config, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: connection },
    });

    await target.runAlpha("control-contract");

    expect(configured.clients[alpha]?.config?.controlContract).toBe("rule-controls-v1");
    expect(transport.httpRequests.filter(item => item.path.endsWith("/rules") || item.path.endsWith("/checks"))
      .map(item => item.controlContract)).toEqual(["rule-controls-v1", "rule-controls-v1", "rule-controls-v1"]);
    configured.uninstall();
  });

  it("marks explicitly mapped object checkpoint inputs as named for pre and post", async () => {
    const target = service();
    const transport = mockConnections();
    const argument = { args: ["application-owned"], state: { qa_blocked: true }, target: "diagnostic-only" };
    const configured = await configure({
      version: 2,
      entrypoints: ["service.runAlpha"],
      checkpoints: {
        send: {
          target: "service.send",
          step_type: "tool",
          input_from: { path: ["args", 0] },
          state_from: { path: ["args", 0, "state"] },
          action: { kind: "other", target: { path: ["args", 0, "target"] } },
        },
      },
    }, {
      agentName: alpha,
      bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha(argument as unknown as string);

    expect(transport.requests.map(({ body }) => ({
      stage: body.stage,
      input: body.step?.input,
      argumentShape: (body.step?.context as Record<string, unknown> | null)?.norma_argument_shape,
      state: (body.step?.context as Record<string, unknown> | null)?.state,
      action: (body.step?.context as Record<string, unknown> | null)?.norma_action,
    }))).toEqual([
      { stage: "pre", input: argument, argumentShape: "named", state: argument.state,
        action: { kind: "other", target: "diagnostic-only", command: null, cwd: null } },
      { stage: "post", input: argument, argumentShape: "named", state: argument.state,
        action: { kind: "other", target: "diagnostic-only", command: null, cwd: null } },
    ]);
    configured.uninstall();
  });

  it.each([
    { label: "scalar", input: "mapped scalar" },
    { label: "array", input: ["mapped", "array"] },
  ])("preserves explicitly mapped $label checkpoint input shape", async ({ input }) => {
    const target = service();
    const transport = mockConnections();
    const configured = await configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send", input_from: { value: input } },
    } }, {
      agentName: alpha, bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha("application-argument");

    expect(transport.requests.map(({ body }) => ({
      stage: body.stage,
      input: body.step?.input,
      argumentShape: (body.step?.context as Record<string, unknown> | null)?.norma_argument_shape,
    }))).toEqual([
      { stage: "pre", input, argumentShape: "positional" },
      { stage: "post", input, argumentShape: "positional" },
    ]);
    configured.uninstall();
  });

  it("keeps mapped object inputs on LLM checkpoints free of tool context markers", async () => {
    const target = service();
    const transport = mockConnections();
    const input = { args: ["application-owned"], request: "mapped LLM input" };
    const configured = await configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send", step_type: "llm", input_from: { value: input } },
    } }, {
      agentName: alpha, bindings: { service: target },
      connections: { [alpha]: transport.connections[alpha] },
    });

    await target.runAlpha("application-argument");

    expect(transport.requests.map(({ body }) => ({ stage: body.stage, input: body.step?.input, context: body.step?.context }))).toEqual([
      { stage: "pre", input, context: null },
      { stage: "post", input, context: null },
    ]);
    configured.uninstall();
  });

  it("binds inherited provider methods to the explicitly supplied binding instance", async () => {
    const target = service(); const transport = mockConnections();
    const providerPrototype = {
      capture(this: { tenant: string }) { return { tenant: this.tenant }; },
    };
    const providers = Object.assign(Object.create(providerPrototype) as { tenant: string }, { tenant: "tenant-a" });
    const configured = await configure({ version: 2, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send", state_from: { provider: "providers.capture" } },
    } }, {
      agentName: alpha, bindings: { service: target, providers },
      connections: { [alpha]: transport.connections[alpha] },
    });
    await expect(target.runAlpha("bound")).resolves.toBe("bound:done");
    expect(transport.requests.find(item => item.body.stage === "pre")?.body.step?.context).toMatchObject({ state: { tenant: "tenant-a" } });
    configured.uninstall();
  });

  it("records and checks an explicit generic turn after the entrypoint returns", async () => {
    const target = service();
    const finishFetch = vi.fn(async () => new Response(JSON.stringify({
      execution_contract: "hosted-v1", verdict: "pass", reason: "Complete", reason_codes: [], held_by: [], would_hold_by: [],
      continuation: { count: 0, max: 2 }, allow_finish: true, outcome: "completed", provider_input_tokens: 0,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : new Response(JSON.stringify({
      confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
    }), { headers: { "content-type": "application/json" } }));
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "promise", turn: {
      session_id: { path: ["args", 0] }, turn_id: { value: "turn-1" }, user_request: { path: ["args", 0] },
      finish: { final_message: { path: ["result"] }, on_hold: "raise" },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] },
    });
    await expect(target.runAlpha("session-1")).resolves.toBe("session-1:done");
    const request = transport.requests.find(item => item.body.step?.type === "turn")?.body;
    expect(request).toMatchObject({
      agent_name: alpha,
      step: { type: "turn", input: { session_id: "session-1", turn_id: "turn-1", final_message: "session-1:done" } },
    });
    configured.uninstall();
  });

  it("finishes a configured async stream once on natural exhaustion and preserves yielded chunks", async () => {
    const firstChunk = { text: "Hello " };
    const secondChunk = { text: "world" };
    const terminalValue = { response_id: "resp-1" };
    const target = {
      async *runAlpha() {
        yield firstChunk;
        yield secondChunk;
        return terminalValue;
      },
    };
    const finishFetch = vi.fn(async () => new Response(JSON.stringify({
      execution_contract: "hosted-v1", verdict: "pass", reason: "Complete", reason_codes: [], held_by: [], would_hold_by: [],
      continuation: { count: 0, max: 0 }, allow_finish: true, outcome: "completed", provider_input_tokens: 0,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : new Response(JSON.stringify({
      confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
    }), { headers: { "content-type": "application/json" } }));
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-1" }, turn_id: { value: "turn-1" },
      finish: { stream: { text: { path: ["result", "text"] } }, on_hold: "raise" },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] },
    });

    const iterable = target.runAlpha();
    expect(iterable).not.toBeInstanceOf(Promise);
    const iterator = iterable[Symbol.asyncIterator]();
    const first = await iterator.next();
    expect(first.value).toBe(firstChunk);
    expect(finishFetch).not.toHaveBeenCalled();
    const second = await iterator.next();
    expect(second.value).toBe(secondChunk);
    expect(finishFetch).not.toHaveBeenCalled();
    const end = await iterator.next();
    expect(end).toEqual({ done: true, value: terminalValue });
    expect(finishFetch).toHaveBeenCalledTimes(1);
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step).toMatchObject({
      input: { session_id: "session-1", turn_id: "turn-1", final_message: "Hello world" },
    });
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
    expect(finishFetch).toHaveBeenCalledTimes(1);
    configured.uninstall();
  });

  it("finishes string deltas only after the last natural advance without a verdict callback", async () => {
    const target = { async *runAlpha() { yield "Good "; yield "morning"; return "provider-terminal"; } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-2" }, turn_id: { value: "turn-2" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect(iterator).not.toBeInstanceOf(Promise);
    expect((await iterator.next()).value).toBe("Good ");
    expect(finishFetch).not.toHaveBeenCalled();
    expect((await iterator.next()).value).toBe("morning");
    expect(finishFetch).not.toHaveBeenCalled();
    expect(await iterator.next()).toEqual({ done: true, value: "provider-terminal" });
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({ final_message: "Good morning" });
    configured.uninstall();
  });

  it("uses the last non-null selected text while skipping only absent paths and nulls", async () => {
    const chunks = [{}, { delta: null }, { delta: "first" }, { delta: "last" }];
    const target = { async *runAlpha() { for (const chunk of chunks) yield chunk; } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-3" }, turn_id: { value: "turn-3" },
      finish: { stream: { text: { path: ["result", "delta"] }, mode: "last", missing: "skip" }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    for (const chunk of chunks) expect((await iterator.next()).value).toBe(chunk);
    await iterator.next();
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({ final_message: "last" });
    configured.uninstall();
  });

  it("resolves provider text against each current chunk and preserves callback failures", async () => {
    const chunks = [{ text: "provider-" }, { text: "selected" }];
    const seen: unknown[] = [];
    const target = { async *runAlpha() { for (const chunk of chunks) yield chunk; } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", context: { value: { request_id: "req-1" } }, turn: {
      session_id: { value: "session-provider" }, turn_id: { value: "turn-provider" },
      finish: { stream: { text: { provider: "callbacks.text" } }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: {
      service: target,
      callbacks: { text: (invocation: { result: unknown; context: unknown; agent_name: string }) => {
        seen.push(invocation.result);
        expect(invocation.context).toEqual({ request_id: "req-1" });
        expect(invocation.agent_name).toBe(alpha);
        return (invocation.result as { text: string }).text;
      } },
    }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe(chunks[0]);
    expect((await iterator.next()).value).toBe(chunks[1]);
    await iterator.next();
    expect(seen).toEqual(chunks);
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({ final_message: "provider-selected" });
    configured.uninstall();

    const callbackFailure = new Error("text provider failed");
    let cleaned = false;
    const failingTarget = { async *runAlpha() { try { yield { text: "ignored" }; } finally { cleaned = true; } } };
    const failingTransport = mockConnections((_name, body) => body.step?.type === "turn" ? finishResponse() : finishResponse());
    const failingConfigured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-provider-error" }, turn_id: { value: "turn-provider-error" },
      finish: { stream: { text: { provider: "callbacks.text" }, missing: "skip" }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: failingTarget, callbacks: { text: () => { throw callbackFailure; } } },
      connections: { [alpha]: failingTransport.connections[alpha] } });
    await expect(failingTarget.runAlpha().next()).rejects.toBe(callbackFailure);
    expect(cleaned).toBe(true);
    expect(failingTransport.requests.filter(item => item.body.step?.type === "turn")).toHaveLength(0);
    failingConfigured.uninstall();
  });

  it("does not skip missing, non-string or accessor-based selected text", async () => {
    let cleaned = false;
    let getterCalls = 0;
    const accessorChunk = Object.defineProperty({}, "text", { get() { getterCalls += 1; return "unsafe"; } });
    const target = { async *runAlpha() { try { yield accessorChunk; } finally { cleaned = true; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-accessor" }, turn_id: { value: "turn-accessor" },
      finish: { stream: { text: { path: ["result", "text"] }, missing: "skip" }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    await expect(target.runAlpha().next()).rejects.toThrow(/accessor/i);
    expect(getterCalls).toBe(0);
    expect(cleaned).toBe(true);
    expect(finishFetch).not.toHaveBeenCalled();
    configured.uninstall();

    const missingTarget = { async *runAlpha() { yield {}; } };
    const missingConfigured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-missing" }, turn_id: { value: "turn-missing" },
      finish: { stream: { text: { path: ["result", "text"] }, missing: "error" }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: missingTarget }, connections: { [alpha]: transport.connections[alpha] } });
    await expect(missingTarget.runAlpha().next()).rejects.toBeInstanceOf(CheckError);
    expect(finishFetch).not.toHaveBeenCalled();
    missingConfigured.uninstall();

    const nonStringTarget = { async *runAlpha() { yield { text: 7 }; } };
    const nonStringConfigured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-nonstring" }, turn_id: { value: "turn-nonstring" },
      finish: { stream: { text: { path: ["result", "text"] }, missing: "skip" }, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: nonStringTarget }, connections: { [alpha]: transport.connections[alpha] } });
    await expect(nonStringTarget.runAlpha().next()).rejects.toBeInstanceOf(CheckError);
    expect(finishFetch).not.toHaveBeenCalled();
    nonStringConfigured.uninstall();
  });

  it("checks empty stream exhaustion with an empty string and exposes held aggregate text", async () => {
    const emptyIterable: AsyncIterable<string> = {
      [Symbol.asyncIterator](): AsyncIterator<string> {
        return { async next(): Promise<IteratorResult<string>> { return { done: true, value: "stream-return-value" }; } };
      },
    };
    const target = { runAlpha() { return emptyIterable; } };
    const finishFetch = vi.fn(async () => finishResponse("hold"));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "async_iterable", turn: {
      session_id: { value: "session-4" }, turn_id: { value: "turn-4" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    await expect(iterator.next()).rejects.toBeInstanceOf(FinishHeldError);
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
    expect(finishFetch).toHaveBeenCalledTimes(1);
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({ final_message: "" });
    configured.uninstall();
  });

  it("accepts exactly 8,000 astral code points and rejects overflow with iterator cleanup", async () => {
    let cleaned = false;
    const atLimit = "💙".repeat(8000);
    const target = {
      async *runAlpha() {
        try { yield atLimit; }
        finally { cleaned = true; }
      },
    };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-5" }, turn_id: { value: "turn-5" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe(atLimit);
    expect(cleaned).toBe(false);
    expect((await iterator.next()).done).toBe(true);
    const turnInput = transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input as { final_message: string };
    expect([...turnInput.final_message]).toHaveLength(8000);
    expect(turnInput.final_message.length).toBe(16000);
    expect(finishFetch).toHaveBeenCalledTimes(1);
    configured.uninstall();

    let overflowCleaned = false;
    const overflowTarget = { async *runAlpha() { try { yield "💙".repeat(8001); } finally { overflowCleaned = true; } } };
    const overflowFetch = vi.fn(async () => finishResponse());
    const overflowTransport = mockConnections((_name, body) => body.step?.type === "turn" ? overflowFetch() : finishResponse());
    const overflowConfigured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-6" }, turn_id: { value: "turn-6" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: overflowTarget }, connections: { [alpha]: overflowTransport.connections[alpha] } });
    const overflowIterator = overflowTarget.runAlpha();
    await expect(overflowIterator.next()).rejects.toBeInstanceOf(CheckError);
    expect(overflowCleaned).toBe(true);
    expect(overflowFetch).not.toHaveBeenCalled();
    expect(await overflowIterator.next()).toEqual({ done: true, value: undefined });
    overflowConfigured.uninstall();
  });

  it("supports sent values and handled throws before one natural streamed finish", async () => {
    const target = {
      async *runAlpha() {
        try {
          const sent = yield "start";
          yield `sent:${String(sent)}`;
        } catch (error) {
          yield `caught:${(error as Error).message}`;
        }
        return "handled-terminal";
      },
    };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-7" }, turn_id: { value: "turn-7" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("start");
    expect((await iterator.next("answer")).value).toBe("sent:answer");
    expect((await iterator.throw(new Error("recover"))).value).toBe("caught:recover");
    expect(await iterator.next()).toEqual({ done: true, value: "handled-terminal" });
    expect(finishFetch).toHaveBeenCalledTimes(1);
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({
      final_message: "startsent:answercaught:recover",
    });
    configured.uninstall();
  });

  it("keeps explicit finish generation metadata on the streamed verdict callback", async () => {
    const target = { async *runAlpha() { yield "stream complete"; } };
    const finishFetch = vi.fn(async () => new Response(JSON.stringify({
      execution_contract: "hosted-v1", agent_generation: "stream-generation",
      verdict: "pass", reason: "Done", reason_codes: [], held_by: [], would_hold_by: [],
      continuation: { count: 0, max: 0, agent_generation: "stream-generation" },
      allow_finish: true, outcome: "completed", provider_input_tokens: 0,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    const observed: Array<{ agent_generation?: string | null; continuation: { count: number; max: number; agent_generation?: string | null } }> = [];
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-stream-generation" }, turn_id: { value: "turn-stream-generation" },
      finish: { stream: {}, on_hold: "raise", on_verdict: { binding: "callbacks.verdict" } },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target, callbacks: { verdict: ({ verdict }: { verdict: {
        agent_generation?: string | null; continuation: { count: number; max: number; agent_generation?: string | null };
      } }) => observed.push(verdict) } }, connections: { [alpha]: transport.connections[alpha] },
    });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("stream complete");
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
    expect(observed[0]).toMatchObject({ agent_generation: "stream-generation", continuation: {
      count: 0, max: 0, agent_generation: "stream-generation",
    } });
    expect(finishFetch).toHaveBeenCalledOnce();
    configured.uninstall();
  });

  it("finishes once when a caught throw completes the generator normally", async () => {
    const target = { async *runAlpha() {
      try { yield "before"; yield "throw-here"; }
      catch { return "handled-return"; }
    } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-caught-return" }, turn_id: { value: "turn-caught-return" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("before");
    expect((await iterator.next()).value).toBe("throw-here");
    expect(await iterator.throw(new Error("handled"))).toEqual({ done: true, value: "handled-return" });
    expect(finishFetch).toHaveBeenCalledTimes(1);
    expect(transport.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({ final_message: "beforethrow-here" });
    configured.uninstall();
  });

  it("preserves an unhandled stream error and never checks its partial text", async () => {
    const failure = new Error("application failed");
    let cleaned = false;
    const target = { async *runAlpha() { try { yield "partial"; throw failure; } finally { cleaned = true; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-8" }, turn_id: { value: "turn-8" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("partial");
    await expect(iterator.next()).rejects.toBe(failure);
    expect(cleaned).toBe(true);
    expect(finishFetch).not.toHaveBeenCalled();
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
    configured.uninstall();
  });

  it("treats return during a pending next as permanent cancellation", async () => {
    let release!: () => void;
    let started!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const entered = new Promise<void>(resolve => { started = resolve; });
    let cleaned = false;
    const target = { async *runAlpha() { try { started(); await pending; yield "late"; } finally { cleaned = true; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-9" }, turn_id: { value: "turn-9" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    const next = iterator.next();
    await entered;
    const returned = iterator.return("cancelled");
    release();
    expect(await next).toMatchObject({ done: false, value: "late" });
    expect(await returned).toEqual({ done: true, value: "cancelled" });
    expect(cleaned).toBe(true);
    expect(finishFetch).not.toHaveBeenCalled();
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
    configured.uninstall();
  });

  it("drains finally-yields and preserves the terminal return without finishing", async () => {
    let cleaned = false;
    const target = { async *runAlpha() { try { yield "start"; } finally { cleaned = true; yield "cleanup-1"; yield "cleanup-2"; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-10" }, turn_id: { value: "turn-10" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("start");
    expect(await iterator.return("cancelled")).toEqual({ done: false, value: "cleanup-1" });
    expect(cleaned).toBe(true);
    expect(await iterator.next()).toEqual({ done: false, value: "cleanup-2" });
    expect(await iterator.next()).toEqual({ done: true, value: "cancelled" });
    expect(finishFetch).not.toHaveBeenCalled();
    configured.uninstall();
  });

  it("does not finish a stream when for-await exits early", async () => {
    let cleaned = false;
    const target = { async *runAlpha() { try { yield "first"; yield "second"; } finally { cleaned = true; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-break" }, turn_id: { value: "turn-break" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    for await (const chunk of target.runAlpha()) {
      expect(chunk).toBe("first");
      break;
    }
    expect(cleaned).toBe(true);
    expect(finishFetch).not.toHaveBeenCalled();
    configured.uninstall();
  });

  it("preserves declared AsyncIterable and Promise<AsyncIterable> call shapes without probing", async () => {
    let directCalls = 0;
    const directTarget = {
      make() {
        directCalls += 1;
        return (async function* () { yield "direct"; })();
      },
    };
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishResponse() : finishResponse());
    const directConfigured = await configure({ version: 2, entrypoints: [{ target: "service.make", returns: "async_iterable", turn: {
      session_id: { value: "session-direct" }, turn_id: { value: "turn-direct" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: directTarget }, connections: { [alpha]: transport.connections[alpha] } });
    expect(directCalls).toBe(0);
    const direct = directTarget.make();
    expect(direct).not.toBeInstanceOf(Promise);
    expect(directCalls).toBe(0);
    expect(await direct.next()).toEqual({ done: false, value: "direct" });
    expect(directCalls).toBe(1);
    expect((await direct.next()).done).toBe(true);
    directConfigured.uninstall();

    let promiseCalls = 0;
    const promiseTarget = {
      make() {
        promiseCalls += 1;
        return Promise.resolve((async function* () { yield "promise"; })());
      },
    };
    const promiseConfigured = await configure({ version: 2, entrypoints: [{ target: "service.make", returns: "promise", turn: {
      session_id: { value: "session-promise" }, turn_id: { value: "turn-promise" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: promiseTarget }, connections: { [alpha]: transport.connections[alpha] } });
    expect(promiseCalls).toBe(0);
    const pending = promiseTarget.make();
    expect(pending).toBeInstanceOf(Promise);
    const promised = await pending;
    expect(promiseCalls).toBe(0);
    expect((await promised.next()).value).toBe("promise");
    expect(promiseCalls).toBe(1);
    expect((await promised.next()).done).toBe(true);
    promiseConfigured.uninstall();

    let nativeCalls = 0;
    const nativeTarget = { async make() { nativeCalls += 1; return (async function* () { yield "native"; })(); } };
    const nativeConfigured = await configure({ version: 2, entrypoints: [{ target: "service.make", turn: {
      session_id: { value: "session-native-promise" }, turn_id: { value: "turn-native-promise" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: nativeTarget }, connections: { [alpha]: transport.connections[alpha] } });
    const nativePending = nativeTarget.make();
    expect(nativePending).toBeInstanceOf(Promise);
    const native = await nativePending;
    expect(nativeCalls).toBe(0);
    expect((await native.next()).value).toBe("native");
    expect(nativeCalls).toBe(1);
    expect((await native.next()).done).toBe(true);
    nativeConfigured.uninstall();
  });

  it("keeps v1 generators and checkpoint generators unsupported before identity reads", async () => {
    const target = {
      async *runAlpha() { yield "entry"; },
      async *send() { yield "checkpoint"; },
    };
    const transport = mockConnections();
    await expect(configure({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] },
    })).rejects.toThrow(/generator/i);
    await expect(configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" }, finish: { final_message: { value: "done" }, on_hold: "raise" },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] },
    })).rejects.toThrow(/generator/i);
    await expect(configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: { send: { target: "service.send" } } }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] },
    })).rejects.toThrow(/generator/i);
    expect(transport.httpRequests).toHaveLength(0);
  });

  it("keeps interleaved streamed agents and their shared checkpoint contexts isolated", async () => {
    const target = {
      async send(value: string) { return value; },
      async *runAlpha() { yield await this.send("alpha-1"); yield await this.send("alpha-2"); },
      async *runBravo() { yield await this.send("bravo-1"); yield await this.send("bravo-2"); },
    };
    const directory = tempDirectory();
    const file = (owner: string, method: string) => JSON.stringify({
      version: 2,
      entrypoints: [{ target: `service.${method}`, context: { value: { owner } }, turn: {
        session_id: { value: `session-${owner}` }, turn_id: { value: `turn-${owner}` }, finish: { stream: {}, on_hold: "raise" },
      } }],
      checkpoints: { send: { target: "service.send", context_from: { path: ["context"] } } },
    });
    writeFileSync(join(directory, `${alpha}.json`), file(alpha, "runAlpha"));
    writeFileSync(join(directory, `${bravo}.json`), file(bravo, "runBravo"));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishResponse() : new Response(JSON.stringify({
      confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
    }), { headers: { "content-type": "application/json" } }));
    const configured = await configure(directory, { bindings: { service: target }, connections: transport.connections });

    const alphaIterator = target.runAlpha();
    const bravoIterator = target.runBravo();
    const [alphaFirst, bravoFirst] = await Promise.all([alphaIterator.next(), bravoIterator.next()]);
    expect(alphaFirst.value).toBe("alpha-1");
    expect(bravoFirst.value).toBe("bravo-1");
    const [alphaSecond, bravoSecond] = await Promise.all([alphaIterator.next(), bravoIterator.next()]);
    expect(alphaSecond.value).toBe("alpha-2");
    expect(bravoSecond.value).toBe("bravo-2");
    await Promise.all([alphaIterator.next(), bravoIterator.next()]);

    const prechecks = transport.requests.filter(item => item.body.stage === "pre" && item.body.step?.name === "send");
    expect(prechecks.map(item => item.agentName)).toEqual(expect.arrayContaining([alpha, bravo]));
    expect(prechecks.filter(item => item.agentName === alpha).every(item => (item.body.step?.context as { owner?: string })?.owner === alpha)).toBe(true);
    expect(prechecks.filter(item => item.agentName === bravo).every(item => (item.body.step?.context as { owner?: string })?.owner === bravo)).toBe(true);
    const finishes = transport.requests.filter(item => item.body.step?.type === "turn");
    expect(finishes.map(item => item.agentName).sort()).toEqual([alpha, bravo].sort());
    configured.uninstall();
  });

  it("serializes concurrent next calls and performs at most one terminal finish", async () => {
    const target = { async *runAlpha() { yield "one"; yield "two"; return "end"; } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-concurrent" }, turn_id: { value: "turn-concurrent" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    const results = await Promise.all([iterator.next(), iterator.next(), iterator.next(), iterator.next()]);
    expect(results).toEqual([
      { done: false, value: "one" }, { done: false, value: "two" }, { done: true, value: "end" }, { done: true, value: undefined },
    ]);
    expect(finishFetch).toHaveBeenCalledTimes(1);
    configured.uninstall();
  });

  it("fences a stream when its lazy context provider resolves after close", async () => {
    let resolveContext!: (value: { request_id: string }) => void;
    let providerStarted!: () => void;
    const started = new Promise<void>(resolve => { providerStarted = resolve; });
    const context = () => { providerStarted(); return new Promise<{ request_id: string }>(resolve => { resolveContext = resolve; }); };
    let factoryCalls = 0;
    const target = { runAlpha() { factoryCalls += 1; return (async function* () { yield "never"; })(); } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "async_iterable",
      context: { provider: "callbacks.context" }, turn: {
        session_id: { value: "session-context-close" }, turn_id: { value: "turn-context-close" }, finish: { stream: {}, on_hold: "raise" },
      } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target, callbacks: { context } }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    const next = iterator.next();
    await started;
    await configured.close();
    resolveContext({ request_id: "late" });
    await expect(next).rejects.toBeInstanceOf(CheckError);
    expect(factoryCalls).toBe(0);
    expect(finishFetch).not.toHaveBeenCalled();
  });

  it("blocks a pending stream advance after close and still runs iterator cleanup", async () => {
    let release!: () => void;
    let entered!: () => void;
    const pending = new Promise<void>(resolve => { release = resolve; });
    const started = new Promise<void>(resolve => { entered = resolve; });
    let cleaned = false;
    const target = { async *runAlpha() { try { entered(); await pending; yield "late"; } finally { cleaned = true; } } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-next-close" }, turn_id: { value: "turn-next-close" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    const next = iterator.next();
    await started;
    await configured.close();
    release();
    await expect(next).rejects.toBeInstanceOf(CheckError);
    expect(cleaned).toBe(true);
    expect(finishFetch).not.toHaveBeenCalled();
  });

  it("allows explicit iterator cleanup after the configured handle closes", async () => {
    let cleaned = false;
    const target = { async *runAlpha() { try { yield "first"; yield "unread"; } finally { cleaned = true; } } };
    const transport = mockConnections();
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-cleanup-close" }, turn_id: { value: "turn-cleanup-close" }, finish: { stream: {}, on_hold: "raise" },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("first");
    await configured.close();
    expect(await iterator.return("shutdown")).toEqual({ done: true, value: "shutdown" });
    expect(cleaned).toBe(true);
    expect(transport.requests.filter(item => item.body.step?.type === "turn")).toHaveLength(0);
  });

  it("blocks streamed finish and its verdict callback when close wins a pending finish check", async () => {
    let resolveFinish!: (response: Response) => void;
    let finishStarted!: () => void;
    const started = new Promise<void>(resolve => { finishStarted = resolve; });
    const pendingResponse = new Promise<Response>(resolve => { resolveFinish = resolve; });
    let callbackCalls = 0;
    const target = { async *runAlpha() { yield "complete"; } };
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? (finishStarted(), pendingResponse) : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-finalize-close" }, turn_id: { value: "turn-finalize-close" },
      finish: { stream: {}, on_hold: "raise", on_verdict: { binding: "callbacks.verdict" } },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target, callbacks: { verdict: () => { callbackCalls += 1; } } },
      connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    expect((await iterator.next()).value).toBe("complete");
    const finalAdvance = iterator.next();
    await started;
    await configured.close();
    resolveFinish(finishResponse());
    await expect(finalAdvance).rejects.toBeInstanceOf(CheckError);
    expect(callbackCalls).toBe(0);
    expect(transport.requests.filter(item => item.body.step?.type === "turn")).toHaveLength(1);
  });

  it("does not retry a streamed verdict callback that resolves after close", async () => {
    let releaseCallback!: () => void;
    let callbackStarted!: () => void;
    const pending = new Promise<void>(resolve => { releaseCallback = resolve; });
    const started = new Promise<void>(resolve => { callbackStarted = resolve; });
    let callbackCalls = 0;
    const target = { async *runAlpha() { yield "complete"; } };
    const finishFetch = vi.fn(async () => finishResponse());
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "session-callback-close" }, turn_id: { value: "turn-callback-close" },
      finish: { stream: {}, on_hold: "raise", on_verdict: { binding: "callbacks.verdict" } },
    } }], checkpoints: {} }, { agentName: alpha, bindings: { service: target, callbacks: { verdict: async () => {
      callbackCalls += 1; callbackStarted(); await pending;
    } } }, connections: { [alpha]: transport.connections[alpha] } });

    const iterator = target.runAlpha();
    await iterator.next();
    const finalAdvance = iterator.next();
    await started;
    await configured.close();
    releaseCallback();
    await expect(finalAdvance).rejects.toBeInstanceOf(CheckError);
    expect(callbackCalls).toBe(1);
    expect(finishFetch).toHaveBeenCalledTimes(1);
    expect(await iterator.next()).toEqual({ done: true, value: undefined });
  });

  it("runs explicit finish continuation without rerunning the entrypoint", async () => {
    const target = service(); let applicationCalls = 0;
    target.runAlpha = function () { applicationCalls += 1; return Promise.resolve("original"); };
    const verdicts: string[] = [];
    const turns: Array<{ continuation?: { agent_generation?: string | null } }> = [];
    const finishFetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ execution_contract: "hosted-v1", agent_generation: "generation-1", verdict: "hold", reason: "Continue", reason_codes: [], held_by: [7], would_hold_by: [], continuation: { count: 0, max: 1, agent_generation: "generation-1" }, allow_finish: false, outcome: "held", provider_input_tokens: 0 }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ execution_contract: "hosted-v1", agent_generation: "generation-1", verdict: "pass", reason: "Done", reason_codes: [], held_by: [], would_hold_by: [], continuation: { count: 1, max: 1, agent_generation: "generation-1" }, allow_finish: true, outcome: "completed", provider_input_tokens: 0 }), { status: 200, headers: { "content-type": "application/json" } }));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : new Response(JSON.stringify({
      confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
    }), { headers: { "content-type": "application/json" } }));
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "promise", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" }, max_continuations: 1,
      finish: { final_message: { path: ["result"] }, on_hold: "raise", continue_with: { binding: "callbacks.continue" }, on_verdict: { binding: "callbacks.verdict" } },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target, callbacks: {
        continue: ({ result, continuation_count }: { result: string; continuation_count: number }) => `${result}:${continuation_count}`,
        verdict: ({ verdict, turn }: { verdict: { verdict: string }; turn: { continuation?: { agent_generation?: string | null } } }) => {
          verdicts.push(verdict.verdict); turns.push(turn);
        },
      } }, connections: { [alpha]: transport.connections[alpha] },
    });
    await expect(target.runAlpha("unused")).resolves.toBe("original:1");
    expect(applicationCalls).toBe(1);
    expect(finishFetch).toHaveBeenCalledTimes(2);
    expect(verdicts).toEqual(["hold", "pass"]);
    expect(turns.map(turn => turn.continuation?.agent_generation)).toEqual([undefined, "generation-1"]);
    const secondRequest = transport.requests.filter(request => request.body.step?.type === "turn")[1]?.body as unknown as {
      step: { input: { continuation: { agent_generation?: string | null } } };
    };
    expect(secondRequest.step.input.continuation.agent_generation).toBe("generation-1");
    configured.uninstall();
  });

  it("omits a null continuation generation from the configured follow-up request", async () => {
    const target = service();
    target.runAlpha = () => Promise.resolve("original");
    const finishFetch = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({
        execution_contract: "hosted-v1", verdict: "hold", reason: "Continue", reason_codes: [], held_by: [7], would_hold_by: [],
        continuation: { count: 0, max: 1, agent_generation: null },
        allow_finish: false, outcome: "held", provider_input_tokens: 0,
      }), { status: 200, headers: { "content-type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        execution_contract: "hosted-v1", verdict: "pass", reason: "Done", reason_codes: [], held_by: [], would_hold_by: [],
        continuation: { count: 1, max: 1 }, allow_finish: true, outcome: "completed", provider_input_tokens: 0,
      }), { status: 200, headers: { "content-type": "application/json" } }));
    const transport = mockConnections((_name, body) => body.step?.type === "turn" ? finishFetch() : finishResponse());
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "promise", turn: {
      session_id: { value: "session-null-generation" }, turn_id: { value: "turn-null-generation" }, max_continuations: 1,
      finish: { final_message: { path: ["result"] }, on_hold: "raise", continue_with: { binding: "callbacks.continue" } },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target, callbacks: {
        continue: ({ result, continuation_count }: { result: string; continuation_count: number }) => `${result}:${continuation_count}`,
      } }, connections: { [alpha]: transport.connections[alpha] },
    });

    await expect(target.runAlpha()).resolves.toBe("original:1");
    expect(finishFetch).toHaveBeenCalledTimes(2);
    const secondRequest = transport.requests.filter(request => request.body.step?.type === "turn")[1]?.body as unknown as {
      step: { input: { continuation: Record<string, unknown> } };
    };
    expect(secondRequest.step.input.continuation).toEqual({ count: 1, max: 1 });
    configured.uninstall();
  });

  it("keeps continuation generations isolated across concurrent agents", async () => {
    const dir = tempDirectory();
    const turnConfig = (target: string) => ({ version: 2, entrypoints: [{ target, returns: "promise", turn: {
      session_id: { value: "session" }, turn_id: { value: "turn" }, max_continuations: 1,
      finish: { final_message: { path: ["result"] }, on_hold: "raise", continue_with: { binding: "callbacks.continue" } },
    } }], checkpoints: { send: { target: "service.send" } } });
    writeFileSync(join(dir, `${alpha}.json`), JSON.stringify(turnConfig("service.runAlpha")));
    writeFileSync(join(dir, `${bravo}.json`), JSON.stringify(turnConfig("service.runBravo")));
    const generationByAgent = new Map([[alpha, "generation-alpha"], [bravo, "generation-bravo"]]);
    const finishCallCount = new Map<string, number>();
    const transport = mockConnections((name, body) => {
      if (body.step?.type !== "turn") return new Response(JSON.stringify({
        confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1",
      }), { status: 200, headers: { "content-type": "application/json" } });
      const generation = generationByAgent.get(name)!;
      const count = finishCallCount.get(name) ?? 0;
      finishCallCount.set(name, count + 1);
      const held = count === 0;
      return new Response(JSON.stringify({
        execution_contract: "hosted-v1", agent_generation: generation,
        verdict: held ? "hold" : "pass", reason: held ? "Continue" : "Done", reason_codes: [],
        held_by: held ? [7] : [], would_hold_by: [],
        continuation: { count, max: 1, agent_generation: generation },
        allow_finish: !held, outcome: held ? "held" : "completed", provider_input_tokens: 0,
      }), { status: 200, headers: { "content-type": "application/json" } });
    });
    const target = service();
    const configured = await configure(dir, { bindings: {
      service: target,
      callbacks: { continue: ({ result, continuation_count }: { result: string; continuation_count: number }) => `${result}:${continuation_count}` },
    }, connections: transport.connections });

    await Promise.all([target.runAlpha("alpha"), target.runBravo("bravo")]);
    const turnRequests = transport.requests.filter(request => request.body.step?.type === "turn");
    for (const agentName of [alpha, bravo]) {
      const request = turnRequests.filter(item => item.agentName === agentName)[1]?.body as unknown as {
        step: { input: { continuation: { agent_generation?: string | null } } };
      };
      expect(request.step.input.continuation.agent_generation).toBe(generationByAgent.get(agentName));
    }
    expect(finishCallCount).toEqual(new Map([[alpha, 2], [bravo, 2]]));
    configured.uninstall();
  });

  it("blocks the original target when an entrypoint context provider resolves after close", async () => {
    const target = service();
    let calls = 0;
    target.runAlpha = function () { calls += 1; return Promise.resolve("ran"); };
    let resolveContext!: (value: { request_id: string }) => void;
    const context = vi.fn(() => new Promise<{ request_id: string }>(resolve => { resolveContext = resolve; }));
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", context: { provider: "callbacks.context" } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target, callbacks: { context } }, connections: { [alpha]: network.connections[alpha] },
    });
    const pending = target.runAlpha();
    await vi.waitFor(() => expect(context).toHaveBeenCalledOnce());
    configured.uninstall();
    resolveContext({ request_id: "late" });
    await expect(pending).rejects.toBeInstanceOf(CheckError);
    expect(calls).toBe(0);
  });

  it("blocks a finish continuation when on_verdict resolves after close", async () => {
    const target = service();
    target.runAlpha = () => Promise.resolve("original");
    let releaseVerdict!: () => void;
    const verdict = vi.fn()
      .mockImplementationOnce(() => new Promise<void>(resolve => { releaseVerdict = resolve; }))
      .mockImplementation(() => undefined);
    const continuation = vi.fn(() => "continued");
    const transport = mockConnections((_name, body) => body.step?.type === "turn"
      ? new Response(JSON.stringify({ execution_contract: "hosted-v1", verdict: "hold", reason: "Continue", reason_codes: [], held_by: [7], would_hold_by: [], continuation: { count: 0, max: 1 }, allow_finish: false, outcome: "held", provider_input_tokens: 0 }), { status: 200, headers: { "content-type": "application/json" } })
      : new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "promise", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" }, max_continuations: 1,
      finish: { final_message: { path: ["result"] }, on_hold: "raise", on_verdict: { binding: "callbacks.verdict" }, continue_with: { binding: "callbacks.continue" } },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target, callbacks: { verdict, continue: continuation } }, connections: { [alpha]: transport.connections[alpha] },
    });
    const pending = target.runAlpha();
    await vi.waitFor(() => expect(verdict).toHaveBeenCalledOnce());
    configured.uninstall();
    releaseVerdict();
    await expect(pending).rejects.toBeInstanceOf(CheckError);
    expect(continuation).not.toHaveBeenCalled();
  });

  it("requires an explicit promise declaration for non-async turn entrypoints without probing them", async () => {
    const target = service(); let calls = 0;
    target.runAlpha = function () { calls += 1; return Promise.resolve("ok"); };
    const network = mockConnections();
    const config = { version: 2 as const, entrypoints: [{ target: "service.runAlpha", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" },
    } }], checkpoints: {} };
    await expect(configure(config, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } }))
      .rejects.toThrow('declare returns: "promise"');
    expect(calls).toBe(0);
    expect(network.httpRequests).toHaveLength(0);

    const configured = await configure({ ...config, entrypoints: [{ ...config.entrypoints[0]!, returns: "promise" }] }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] },
    });
    expect(calls).toBe(0);
    await expect(target.runAlpha()).resolves.toBe("ok");
    expect(calls).toBe(1);
    configured.uninstall();
  });

  it("reports a false promise declaration after one invocation and never retries", async () => {
    const target = service(); let calls = 0;
    target.runAlpha = function () { calls += 1; return "sync" as never; };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.runAlpha", returns: "promise", turn: {
      session_id: { value: "s" }, turn_id: { value: "t" },
    } }], checkpoints: {} }, {
      agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] },
    });
    await expect(target.runAlpha()).rejects.toThrow('declared returns: "promise" but returned a non-Promise value');
    expect(calls).toBe(1);
    expect(network.httpRequests.filter(request => request.path.endsWith("/finish"))).toHaveLength(0);
    configured.uninstall();
  });

  it("routes a retained operation through the scoped agent and preserves receiver and result", async () => {
    const target = {
      tenant: "tenant-a", calls: 0,
      run(this: { write(value: string): Promise<string> }) { return this.write("value"); },
      write(this: { tenant: string; calls: number }, value: string) { this.calls += 1; return Promise.resolve(`${this.tenant}:${value}`); },
    };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: [{ target: "service.run", target_scope: {
      type: { value: "tenant" }, id: { value: "scope-a" },
    } }], checkpoints: {}, operations: {
      write: { target: "service.write", returns: "promise", operation_key: { value: "operation-1" } },
    } }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    const create = vi.spyOn(HostedOperationsApi.prototype, "create").mockResolvedValue({
      decision: { allowed: true, checks: [], reason: "ok", recoveryFeedback: [] },
    } as never);
    await expect(target.run()).resolves.toBe("tenant-a:value");
    expect(target.calls).toBe(1);
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      operationKey: "operation-1", targetType: "tenant", targetId: "scope-a",
      step: expect.objectContaining({ name: "write", input: { args: ["value"], kwargs: {} } }),
    }));
    configured.uninstall();
  });

  it("resolves retained operation params and input for proposals, action evidence, and finish checks", async () => {
    const target = {
      async run(this: { save(request: { id: string; payload: { text: string }; file: string; command: string }): Promise<string> },
        request: { id: string; payload: { text: string }; file: string; command: string }) {
        return this.save(request);
      },
      async save(request: { payload: { text: string } }) { return request.payload.text; },
    };
    const network = mockConnections((_name, body) => body.step?.type === "turn" ? finishResponse() : finishResponse());
    const configured = await configure({
      version: 2,
      entrypoints: [{ target: "service.run", turn: {
        session_id: { value: "session-operation" }, turn_id: { value: "turn-operation" },
        finish: { final_message: { path: ["result"] }, on_hold: "raise" },
      } }],
      checkpoints: {},
      operations: { save: {
        target: "service.save",
        returns: "promise",
        parameter_names: ["request"],
        operation_key: { path: ["params", "request", "id"] },
        input: { path: ["input", "payload"] },
        action: {
          kind: "edit",
          target: { path: ["params", "request", "file"] },
          command: { path: ["input", "command"] },
        },
      } },
    }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    const create = vi.spyOn(HostedOperationsApi.prototype, "create").mockResolvedValue({
      id: "operation-1", operationKey: "operation-1",
      decision: { allowed: true, checks: [], reason: "ok", recoveryFeedback: [] },
    } as never);

    await expect(target.run({ id: "operation-1", payload: { text: "saved" }, file: "notes.md", command: "write" }))
      .resolves.toBe("saved");

    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      operationKey: "operation-1",
      step: expect.objectContaining({ input: { text: "saved" } }),
    }));
    expect(network.requests.find(item => item.body.step?.type === "turn")?.body.step?.input).toMatchObject({
      final_message: "saved",
      actions: [{ kind: "edit", native_tool: "save", target: "notes.md", command: "write", status: "succeeded" }],
    });
    configured.uninstall();
  });

  it("blocks a retained operation before its effect when the existing decision denies", async () => {
    const target = { run() { return "entry"; }, calls: 0, async write() { this.calls += 1; return "done"; } };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: ["service.run"], checkpoints: {}, operations: {
      write: { target: "service.write", operation_key: { value: "operation-2" } },
    } }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    vi.spyOn(HostedOperationsApi.prototype, "create").mockResolvedValue({
      decision: { allowed: false, checks: [], reason: "denied", recoveryFeedback: [] },
    } as never);
    await expect(configured.withAgent(alpha, () => target.write())).rejects.toThrow("Execution blocked for safety.");
    expect(target.calls).toBe(0);
    configured.uninstall();
  });

  it("uses the approved argument snapshot and blocks effects after operation admission closes", async () => {
    const target = {
      calls: [] as string[],
      run(this: { write(value: { label: string }): Promise<string> }, value: { label: string }) { return this.write(value); },
      async write(this: { calls: string[] }, value: { label: string }) { this.calls.push(value.label); return value.label; },
    };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: ["service.run"], checkpoints: {}, operations: {
      write: { target: "service.write", operation_key: { value: "operation-snapshot" } },
    } }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    let resolveCreate!: (value: unknown) => void;
    vi.spyOn(HostedOperationsApi.prototype, "create").mockImplementation(() => new Promise(resolve => { resolveCreate = resolve; }) as never);
    const callerValue = { label: "approved" };
    const pending = target.run(callerValue);
    callerValue.label = "mutated-after-start";
    await vi.waitFor(() => expect(resolveCreate).toBeTypeOf("function"));
    resolveCreate({ decision: { allowed: true, checks: [], reason: "ok", recoveryFeedback: [] } });
    await expect(pending).resolves.toBe("approved");
    expect(target.calls).toEqual(["approved"]);
    configured.uninstall();
  });

  it("blocks a pending operation when its installation closes during admission", async () => {
    const target = { calls: 0, run() { return this.write(); }, async write() { this.calls += 1; return "done"; } };
    const network = mockConnections();
    const configured = await configure({ version: 2, entrypoints: ["service.run"], checkpoints: {}, operations: {
      write: { target: "service.write", operation_key: { value: "operation-close" } },
    } }, { agentName: alpha, bindings: { service: target }, connections: { [alpha]: network.connections[alpha] } });
    let resolveCreate!: (value: unknown) => void;
    vi.spyOn(HostedOperationsApi.prototype, "create").mockImplementation(() => new Promise(resolve => { resolveCreate = resolve; }) as never);
    const pending = target.run();
    await vi.waitFor(() => expect(resolveCreate).toBeTypeOf("function"));
    configured.uninstall();
    resolveCreate({ decision: { allowed: true, checks: [], reason: "ok", recoveryFeedback: [] } });
    await expect(pending).rejects.toThrow("changed during operation evaluation");
    expect(target.calls).toBe(0);
  });

  it.each([
    { label: "Python transport", connection: { transport: { binding: "transports.http" } } },
    { label: "Python telemetry sink", telemetry: { enabled: true, sink_name: "custom" } },
    { label: "Python logging", telemetry: { logging: { log_enabled: true } } },
    { label: "Python refresh cache", refresh: { interval_seconds: 30 } },
  ])("rejects unsupported v2 $label settings before identity reads or target mutation", async ({ connection, telemetry, refresh }) => {
    const target = service();
    const network = mockConnections();
    const connections = { [alpha]: network.connections[alpha] };
    const { httpRequests } = network;
    const candidate = {
      version: 2 as const,
      entrypoints: ["service.runAlpha"],
      checkpoints: { send: { target: "service.send" } },
      ...(connection ? { connection } : {}),
      ...(telemetry ? { telemetry } : {}),
      ...(refresh ? { refresh } : {}),
    } as unknown as InstrumentationConfig;
    const result = configure(candidate, { agentName: alpha, bindings: { service: target, transports: { http: {} } }, connections })
      .then(handle => { handle.uninstall(); return handle; });
    await expect(result).rejects.toThrow(/not supported by the TypeScript SDK/);
    expect(httpRequests).toHaveLength(0);
    await expect(target.send("still-original")).resolves.toBe("still-original:done");
  });

  it("restores nested agent scopes, preserves receiver, and blocks a target absent from the active agent", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), config("service.runAlpha", "service.send"));
    writeFileSync(join(dir, `${bravo}.json`), JSON.stringify({ version: 1, entrypoints: ["service.runBravo"], checkpoints: { send: { target: "service.send" } } }));
    const target = service();
    const transport = mockConnections();
    target.runAlpha = function (this: typeof target) { return this.runBravo("inner").then(() => this.send("outer")); };
    const configured = await configure(dir, { bindings: { service: target }, connections: transport.connections });
    await expect(target.runAlpha("unused")).resolves.toBe("outer:done");
    expect(transport.requests.filter(item => item.body.stage === "pre").map(item => item.agentName)).toEqual([bravo, alpha]);
    configured.uninstall();
  });

  it("composes a parent checkpoint around a child entrypoint and its own checkpoint", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), JSON.stringify({ version: 2, entrypoints: [{ target: "service.runAlpha", target_scope: {
      type: { value: "tenant" }, id: { path: ["args", 0] },
    }}], checkpoints: {
      child: { target: "service.runBravo" },
    } }));
    writeFileSync(join(dir, `${bravo}.json`), JSON.stringify({ version: 2, entrypoints: [{ target: "service.runBravo", context: { value: { child: true } }, target_scope: {
      type: { value: "repository" }, id: { value: "bravo-default" },
    }}], checkpoints: {
      child: { target: "service.runBravo" },
    } }));
    const target = service();
    target.runAlpha = function (this: typeof target) { return this.runBravo("nested"); };
    target.runBravo = function (this: typeof target, value: string) { this.calls.push("child-body"); return this.send(value); };
    let deny: string | undefined = alpha;
    const transport = mockConnections((name, body) => name === deny && body.stage === "pre"
      ? new Response(JSON.stringify({ confidence: 1, allowed: false, matches: [{ action: "deny", rule_id: 7, rule_name: "blocked", result: { matched: true, confidence: 1 } }], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } })
      : new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    const configured = await configure(dir, { bindings: { service: target }, connections: transport.connections });
    await expect(target.runAlpha("unused")).rejects.toBeInstanceOf(RuleViolationError);
    expect(transport.requests.map(item => item.agentName)).toEqual([alpha]);
    expect(target.calls).toEqual([]);
    transport.requests.length = 0;
    deny = undefined;
    await expect(target.runAlpha("unused")).resolves.toBe("nested:done");
    expect(transport.requests.filter(item => item.body.stage === "pre").map(item => item.agentName)).toEqual([alpha, bravo]);
    expect(transport.requests.filter(item => item.body.stage === "post").map(item => item.agentName)).toEqual([bravo, alpha]);
    expect(transport.requests.filter(item => item.body.stage === "pre").map(item => [item.body.target_type, item.body.target_id])).toEqual([["tenant", "unused"], ["repository", "bravo-default"]]);
    expect(transport.requests.filter(item => item.body.stage === "post").map(item => [item.body.target_type, item.body.target_id])).toEqual([["repository", "bravo-default"], ["tenant", "unused"]]);
    expect(target.calls).toEqual(["child-body", "nested"]);
    transport.requests.length = 0;
    target.calls.length = 0;
    deny = bravo;
    await expect(target.runAlpha("unused")).rejects.toBeInstanceOf(RuleViolationError);
    expect(transport.requests.filter(item => item.body.stage === "pre").map(item => item.agentName)).toEqual([alpha, bravo]);
    expect(transport.requests.filter(item => item.body.stage === "post")).toHaveLength(0);
    expect(target.calls).toEqual([]);
    configured.uninstall();
  });

  it("rejects invalid later files and unauthorized agents before changing any binding", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), config("service.runAlpha", "service.send"));
    writeFileSync(join(dir, `${bravo}.json`), "{\"version\":1,\"entrypoints\":[\"service.runBravo\"],\"checkpoints\":{\"send\":{\"target\":\"service.missing\"}}}");
    const target = service(); const original = target.send; const network = mockConnections();
    await expect(configure(dir, { bindings: { service: target }, connections: network.connections })).rejects.toThrow(/target|function/i);
    expect(target.send).toBe(original);
    expect(network.httpRequests).toHaveLength(0);
    rmSync(join(dir, `${bravo}.json`));
    writeFileSync(join(dir, `${bravo}.json`), config("service.runBravo", "service.send"));
    network.connections[bravo].httpClient = new HTTPClient({ fetcher: async () => new Response("{}", { status: 404 }) });
    await expect(configure(dir, { bindings: { service: target }, connections: network.connections })).rejects.toThrow();
    expect(target.send).toBe(original);
    expect(network.httpRequests.every(item => item.method === "GET")).toBe(true);
  });

  it("captures literal state once and forwards native action, target and custom step type", async () => {
    const target = service();
    const state = { phase: "before" };
    let captureCount = 0;
    const connection = mockConnections();
    const options = {
      meta: { action: (value: string) => ({ kind: "other" as const, target: value }), targetType: "repo", targetId: "main" },
    };
    const configured = await configure({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: {
      send: { target: "service.send", step_type: "custom_tool", state, options: "meta" },
      capture: { target: "service.onlyAlpha", state_provider: "capture" },
    } }, {
      agentName: alpha, bindings: { service: target }, connection: connection.connections[alpha], options,
      providers: { capture: () => { captureCount++; return { phase: "dynamic" }; } },
    });
    state.phase = "after";
    await expect(target.runAlpha("hello")).resolves.toBe("hello:done");
    await expect(configured.withAgent(alpha, () => target.onlyAlpha("dynamic"))).resolves.toBe("dynamic");
    expect(connection.requests[0]?.body).toMatchObject({
      step: { type: "custom_tool", name: "send", context: { state: { phase: "before" }, norma_action: { target: "hello" } } },
      target_type: "repo", target_id: "main",
    });
    const dynamicChecks = connection.requests.filter(item => item.body.step.name === "capture");
    expect(captureCount).toBe(1);
    expect(dynamicChecks).toHaveLength(2);
    expect(dynamicChecks[0]?.body.step.context).toMatchObject({ state: { phase: "dynamic" } });
    expect(dynamicChecks[1]?.body.step.context).toMatchObject({ state: { phase: "dynamic" } });
    configured.uninstall();
  });

  it("rejects frozen, accessor, duplicate and generator targets before mutation", async () => {
    const target = { *run() { yield 1; }, regular() { return "ok"; } };
    const frozen = Object.freeze({ run() { return "ok"; } });
    const network = mockConnections();
    await expect(configure({ version: 1, entrypoints: ["target.regular"], checkpoints: { x: { target: "target.run" } } }, {
      agentName: alpha, bindings: { target }, connection: network.connections[alpha],
    })).rejects.toThrow(/generator/i);
    await expect(configure({ version: 1, entrypoints: ["frozen.run"], checkpoints: {} }, {
      agentName: alpha, bindings: { frozen }, connection: network.connections[alpha],
    })).rejects.toThrow(/writable/i);
    expect(target.run).toBeDefined();
    const accessor = Object.defineProperty({}, "run", { get() { throw new Error("must not run"); } });
    await expect(configure({ version: 1, entrypoints: ["accessor.run"], checkpoints: {} }, {
      agentName: alpha, bindings: { accessor }, connection: network.connections[alpha],
    })).rejects.toThrow(/data properties/i);
  });

  it("shadows inherited methods on the bound instance and marks installed wrappers", async () => {
    class Service {
      run() { return "original"; }
    }
    const instance = new Service();
    const other = new Service();
    const original = Service.prototype.run;
    const network = mockConnections();
    const configured = await configure({ version: 1, entrypoints: ["service.run"], checkpoints: {} }, {
      agentName: alpha, bindings: { service: instance }, connection: network.connections[alpha],
    });
    expect(Service.prototype.run).toBe(original);
    expect(Object.hasOwn(instance, "run")).toBe(true);
    expect(other.run()).toBe("original");
    await expect(configure({ version: 1, entrypoints: ["service.run"], checkpoints: {} }, {
      agentName: alpha, bindings: { service: instance }, connection: network.connections[alpha],
    })).rejects.toThrow(/already a Norma checkpoint wrapper/i);
    configured.uninstall();
    expect(Object.hasOwn(instance, "run")).toBe(false);
    expect(instance.run()).toBe("original");
  });

  it("keeps separate installations with the same agent name isolated", async () => {
    const spec = { version: 1 as const, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send" } } };
    const left = service(); const right = service(); const network = mockConnections();
    const first = await configure(spec, { agentName: alpha, bindings: { service: left }, connection: network.connections[alpha] });
    const second = await configure(spec, { agentName: alpha, bindings: { service: right }, connection: network.connections[alpha] });
    await expect(first.withAgent(alpha, () => right.send("cross"))).rejects.toBeInstanceOf(CheckError);
    await expect(right.runAlpha("right")).resolves.toBe("right:done");
    expect(right.calls).toEqual(["right"]);
    first.uninstall(); second.uninstall();
  });

  it("blocks a pending invocation if its configuration is uninstalled or reinitialized during precheck", async () => {
    const target = service();
    let release!: (response: Response) => void;
    let reached!: () => void;
    const pending = new Promise<Response>(resolve => { release = resolve; });
    const checkReached = new Promise<void>(resolve => { reached = resolve; });
    const transport = mockConnections((_, body) => {
      if (body.stage === "pre") { reached(); return pending; }
      return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
    });
    const configured = await configure({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send" } } }, {
      agentName: alpha, bindings: { service: target }, connection: transport.connections[alpha],
    });
    const run = target.runAlpha("blocked");
    await checkReached;
    configured.uninstall();
    release(new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    await expect(run).rejects.toBeInstanceOf(CheckError);
    expect(target.calls).toEqual([]);
  });

  it("blocks an action when the owning client is reinitialized while precheck is pending", async () => {
    const target = service();
    let release!: (response: Response) => void;
    let reached!: () => void;
    const pending = new Promise<Response>(resolve => { release = resolve; });
    const checkReached = new Promise<void>(resolve => { reached = resolve; });
    const transport = mockConnections((_, body) => {
      if (body.stage === "pre") { reached(); return pending; }
      return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
    });
    const configured = await configure({ version: 1, entrypoints: ["service.runAlpha"], checkpoints: { send: { target: "service.send" } } }, {
      agentName: alpha, bindings: { service: target }, connection: transport.connections[alpha],
    });
    const run = target.runAlpha("stale");
    await checkReached;
    configured.clients[alpha]!.init({ agentName: "agent-replaced", runtimeUrl: "http://localhost:8080/agent-replaced", agentKey: "replacement-key" });
    release(new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } }));
    await expect(run).rejects.toBeInstanceOf(CheckError);
    expect(target.calls).toEqual([]);
    configured.uninstall();
  });

  it("keeps background async work in its entrypoint scope until the handle closes", async () => {
    const dir = tempDirectory();
    writeFileSync(join(dir, `${alpha}.json`), config("service.runAlpha", "service.send"));
    const target = service();
    let release!: () => void;
    const gate = new Promise<void>(resolve => { release = resolve; });
    let activeGate = gate;
    let useCapturedTarget = false;
    let background!: Promise<string>;
    target.runAlpha = function (this: typeof target, value: string) {
      void value;
      const send = this.send;
      background = (async () => {
        await activeGate;
        const value = activeGate === gate ? "background" : "closed";
        return useCapturedTarget ? send.call(this, value) : this.send(value);
      })();
      return "started";
    };
    const transport = mockConnections();
    const configured = await configure(dir, { bindings: { service: target }, connections: { [alpha]: transport.connections[alpha] } });

    expect(target.runAlpha("unused")).toBe("started");
    release();
    await expect(background).resolves.toBe("background:done");
    expect(transport.requests.filter(item => item.body.stage === "pre").map(item => item.agentName)).toEqual([alpha]);

    let releaseAfterClose!: () => void;
    const closeGate = new Promise<void>(resolve => { releaseAfterClose = resolve; });
    activeGate = closeGate;
    useCapturedTarget = true;
    expect(target.runAlpha("unused")).toBe("started");
    configured.uninstall();
    const requestsBefore = transport.requests.length;
    releaseAfterClose();
    await expect(background).rejects.toBeInstanceOf(CheckError);
    expect(transport.requests).toHaveLength(requestsBefore);
    expect(target.calls).toEqual(["background"]);
  });
});
