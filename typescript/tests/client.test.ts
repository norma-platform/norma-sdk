// Modified for Norma.
import { afterEach, describe, expect, it, vi } from "vitest";
import norma, { NormaClient, HTTPClient } from "../src/index";
import { ControlEvidenceSnapshot$inboundSchema } from "../src/generated/models/control-evidence";
import { isControlContractRoute } from "../src/control-contract";
afterEach(() => vi.unstubAllEnvs());
describe("hosted configuration", () => {
  it("opts in only on reviewed check-run context and evidence routes", () => {
    expect(isControlContractRoute("POST", "/api/v1/check-runs/shadow")).toBe(true);
    expect(isControlContractRoute("GET", "/api/v1/check-runs")).toBe(true);
    expect(isControlContractRoute("GET", "/api/v1/check-runs/run-1")).toBe(true);
    expect(isControlContractRoute("POST", "/api/v1/check-runs/run-1")).toBe(false);
    expect(isControlContractRoute("GET", "/api/v1/operations/observe-metrics")).toBe(false);
    expect(isControlContractRoute("POST", "/api/v1/operations/observe-metrics")).toBe(false);
    expect(isControlContractRoute("GET", "/api/v1/agents/app")).toBe(false);
  });
  it("uses only connection settings and explicit application identity", () => {
    vi.stubEnv("NORMA_RUNTIME_URL", "https://tenant.example/api/runtime");
    vi.stubEnv("NORMA_AGENT_KEY", "synthetic-key");
    const client = new NormaClient(); client.init({ agentName: "app" });
    expect(client.config).toMatchObject({agentName:"app",runtimeUrl:"https://tenant.example/api/runtime"});
    expect(JSON.stringify(client.config)).not.toContain("synthetic-key");
    expect(norma).toBeInstanceOf(NormaClient);
    expect(() => client.init({agentName:""})).toThrow(/agentName/);
  });
  it.each(["http://remote.example", "https://user:pass@example.com", "https://example.com?key=x", "https://example.com#x"])("rejects unsafe endpoint %s", runtimeUrl => {
    expect(() => new NormaClient().init({agentName:"app",agentKey:"synthetic",runtimeUrl})).toThrow(/HTTPS/);
  });
  it("requires a nonempty key and URL", () => {
    vi.stubEnv("NORMA_RUNTIME_URL", ""); vi.stubEnv("NORMA_AGENT_KEY", "");
    expect(() => new NormaClient().init({agentName:"app"})).toThrow(/NORMA_RUNTIME_URL/);
    expect(() => new NormaClient().init({agentName:"app",runtimeUrl:"https://tenant.example"})).toThrow(/NORMA_AGENT_KEY/);
  });
  it("preserves the tenant path, uses fixed auth, rejects redirects and endpoint overrides", async () => {
    const requests: Request[]=[];
    const httpClient=new HTTPClient({fetcher:async request => {requests.push(request); return new Response(JSON.stringify({agents:[],pagination:{has_more:false,limit:20}}),{headers:{"content-type":"application/json"}});}});
    const client=new NormaClient(); client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://tenant.example/api/runtime",httpClient});
    await client.agents.list();
    expect(requests[0].url).toBe("https://tenant.example/api/runtime/api/v1/agents");
    expect(requests[0].redirect).toBe("error");
    expect(requests[0].headers.get("X-API-Key")).toBe("synthetic");
    await expect(client.agents.list({}, {serverURL:"https://other.example"})).rejects.toThrow(/override/);
    expect(requests).toHaveLength(1);
    client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://next.example",httpClient});
    await client.agents.list();
    expect(requests[1].url).toBe("https://next.example/api/v1/agents");
  });
  it.each([
    ["header-and-envelope", "rule-controls-v1", {execution_contract:"hosted-v1",control_contract:"rule-controls-v1",allowed:true,confidence:1}, true],
    ["header-only", "rule-controls-v1", {execution_contract:"hosted-v1",allowed:true,confidence:1}, false],
    ["envelope-only", null, {execution_contract:"hosted-v1",control_contract:"rule-controls-v1",allowed:true,confidence:1}, false],
    ["unknown-envelope", "rule-controls-v1", {execution_contract:"hosted-v1",control_contract:"other",allowed:true,confidence:1}, false],
    ["mismatch", "other", {execution_contract:"hosted-v1",control_contract:"rule-controls-v1",allowed:true,confidence:1}, false],
  ])("requires both control acknowledgements (%s)", async (_label, header, payload, accepted) => {
    const requests: Request[]=[];
    const httpClient=new HTTPClient({fetcher:async request => {
      requests.push(request);
      return new Response(JSON.stringify(payload), {headers:{"content-type":"application/json",...(header ? {"X-Norma-Control-Contract":header} : {})}});
    }});
    const client=new NormaClient(); client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://tenant.example",httpClient,controlContract:"rule-controls-v1"});
    const pending=client.checks.check({stage:"pre",step:{type:"tool",name:"read",input:{}}});
    if (accepted) await expect(pending).resolves.toMatchObject({allowed:true});
    else await expect(pending).rejects.toThrow(/control contract/i);
    expect(requests).toHaveLength(1);
    expect(requests[0].headers.get("X-Norma-Control-Contract")).toBe("rule-controls-v1");
  });
  it("keeps the control contract disabled by default", async () => {
    const requests: Request[]=[];
    const httpClient=new HTTPClient({fetcher:async request => {
      requests.push(request);
      return new Response(JSON.stringify({execution_contract:"hosted-v1",allowed:true,confidence:1}), {headers:{"content-type":"application/json"}});
    }});
    const client=new NormaClient(); client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://tenant.example",httpClient});
    await expect(client.checks.check({stage:"pre",step:{type:"tool",name:"read",input:{}}})).resolves.toMatchObject({allowed:true});
    expect(requests).toHaveLength(1);
    expect(requests[0].headers.has("X-Norma-Control-Contract")).toBe(false);
  });
  it("retains typed frozen receipts and evidence on the public decision", async () => {
    const receipt = {
      contract:"rule-controls-v1", capabilities_fingerprint:"a".repeat(64), declaration_fingerprint:"b".repeat(64),
      defaults:{age:90}, effective_values:{age:120}, values_fingerprint:"c".repeat(64),
      value_sources:{age:"agent_override"}, references:[], overridden_keys:["age"],
      agent_generation:"generation-1", agent_name:"app", rule_id:7, rule_version:3, override_revision:4,
      effective_fingerprint:"d".repeat(64),
    };
    const payload = {execution_contract:"hosted-v1", control_contract:"rule-controls-v1", agent_generation:"generation-1",
      control_resolutions:[receipt], control_evidence:[{schema_version:1, completeness:"incomplete", agent_name:"app",
        agent_generation:"generation-1", rule_id:7, rule_version:3, definition_fingerprint:"d".repeat(64),
        receipt, references:[]}], allowed:true, confidence:1};
    const httpClient = new HTTPClient({fetcher:async () => new Response(JSON.stringify(payload), {headers:{
      "content-type":"application/json", "X-Norma-Control-Contract":"rule-controls-v1",
    }})});
    const client = new NormaClient(); client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://tenant.example",
      controlContract:"rule-controls-v1",httpClient});
    const result = await client.checks.check({stage:"pre",step:{type:"tool",name:"read",input:{}}});
    expect(result.controlResolutions?.[0].effectiveValues).toEqual({age:120});
    expect(result.controlEvidence?.[0].receipt?.valueSources).toEqual({age:"agent_override"});
    expect(Object.isFrozen(result.controlEvidence)).toBe(true);
    expect(Object.isFrozen(result.controlEvidence?.[0]?.references)).toBe(true);
    expect(Reflect.set(result.controlEvidence?.[0] ?? {}, "completeness", "complete")).toBe(false);
  });
  it("rejects boolean numeric evidence and freezes nested control values", () => {
    const receiptReference = {key:"age",source_pointer:"/condition/evaluator/config/min",
      destination_pointer:"/condition/evaluator/config/min",evaluator_pointer:"/condition/evaluator",slot:{}};
    const receipt = {contract:"rule-controls-v1",capabilities_fingerprint:"a".repeat(64),
      declaration_fingerprint:"b".repeat(64),defaults:{age:1},effective_values:{age:2},
      values_fingerprint:"c".repeat(64),value_sources:{age:"agent_override"},references:[receiptReference],
      overridden_keys:["age"],agent_generation:"generation-1",agent_name:"app",rule_id:7,
      rule_version:3,override_revision:1,effective_fingerprint:"d".repeat(64)};
    const evidence = ControlEvidenceSnapshot$inboundSchema.parse({ schema_version: 1, completeness: "incomplete",
      agent_name: "app", agent_generation: "generation-1", rule_id: 7, rule_version: 3,
      definition_fingerprint: "d".repeat(64), receipt, references: [{ key: "age", destination_pointer: "/condition/evaluator/config/min",
        configured_default: 1, effective_value: 2, value_source: "agent_override", disposition: "evaluated",
        numeric_comparisons: [{ field_path: "age", operator: "min", observed: 21, expected: 20, outcome: "passed" }] }] });
    expect(Object.isFrozen(evidence)).toBe(true);
    expect(Object.isFrozen(evidence.references[0]?.numericComparisons)).toBe(true);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({ schema_version: 1, completeness: "incomplete",
      agent_name: "app", agent_generation: "generation-1", rule_id: 7, definition_fingerprint: "d".repeat(64),
      rule_version:3,receipt,references: [{ key: "age", destination_pointer: "/condition/evaluator/config/min",
        configured_default: 1, effective_value: 2, value_source: "agent_override", disposition: "evaluated",
        numeric_comparisons: [{ field_path: "age", operator: "min", observed: true,
          expected: 2, outcome: "passed" }] }] })).toThrow();
    const reference = {key:"age",destination_pointer:"/condition/evaluator/config/min",configured_default:1,
      effective_value:2,value_source:"agent_override",disposition:"evaluated",numeric_comparisons:[]};
    const valid = {schema_version:1,completeness:"incomplete",agent_name:"app",agent_generation:"generation-1",
      rule_id:7,rule_version:3,definition_fingerprint:"d".repeat(64),receipt,references:[reference]};
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, references:[reference,reference]})).toThrow(/unique/);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, references:[{...reference,configured_default:true}]})).toThrow(/match/);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, references:[{...reference,configured_default:"1"}]})).toThrow(/match/);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, references:[{...reference,effective_value:false}]})).toThrow(/match/);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, receipt:{...receipt,defaults:{}}})).toThrow(/missing/);
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, rule_id:7.5})).toThrow();
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, rule_id:true})).toThrow();
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, rule_id:0})).toThrow();
    expect(() => ControlEvidenceSnapshot$inboundSchema.parse({...valid, receipt:undefined})).toThrow(/require a receipt/);
  });
  it("negotiates effective registration and rule read adapters", async () => {
    const requests: Request[]=[];
    const httpClient=new HTTPClient({fetcher:async request => {
      requests.push(request);
      const payload = request.url.endsWith("/agents/register")
        ? {created:false,overwrite_applied:false,rules:[],control_contract:"rule-controls-v1"}
        : {rules:[],control_contract:"rule-controls-v1"};
      return new Response(JSON.stringify(payload), {headers:{"content-type":"application/json","X-Norma-Control-Contract":"rule-controls-v1"}});
    }});
    const client=new NormaClient(); client.init({agentName:"app",agentKey:"synthetic",runtimeUrl:"https://tenant.example",httpClient,controlContract:"rule-controls-v1"});
    await client.agents.register({agent:{agentName:"app"},steps:[]});
    await client.agents.listRules({agentName:"app"});
    expect(requests.map(request => request.headers.get("X-Norma-Control-Contract"))).toEqual(["rule-controls-v1","rule-controls-v1"]);
    expect(requests.map(request => new URL(request.url).pathname)).toEqual(["/api/v1/agents/register","/api/v1/agents/app/rules"]);
  });
  it("does not expose management groups", () => {
    const client=new NormaClient();
    for (const key of ["auth","system","policies","rules","rules"]) expect(client).not.toHaveProperty(key);
  });
});
