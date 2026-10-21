// Modified for Norma.
import {describe, expect, it} from "vitest";
import {HTTPClient, NormaClient} from "../src/index";

describe("native protocol traffic", () => {
  it("uses native registration, Rules and Checks paths and payloads", async () => {
    const traffic: Array<{path: string; method: string; body: unknown}> = [];
    const transport = new HTTPClient({fetcher: async request => {
      const path = new URL(request.url).pathname;
      const body = request.method === "GET" ? null : await request.json();
      traffic.push({path, method: request.method, body});
      const result = path.endsWith("/register") ? {created: false}
        : path.endsWith("/rules") ? {rules: []}
        : {allowed: true, confidence: 1, execution_contract: "hosted-v1", non_matches: [{
          rule_id: 7, rule_name: "limits", check_id: "native-check-7", action: "observe",
          result: {matched: false, confidence: 1},
        }]};
      return new Response(JSON.stringify(result), {headers: {"content-type": "application/json"}});
    }});
    const client = new NormaClient();
    client.init({agentName: "native-agent", runtimeUrl: "https://tenant.example/api/runtime",
      agentKey: "synthetic", httpClient: transport, observabilityEnabled: false});
    await client.agents.register({agent: {agentName: "native-agent"}, steps: []});
    expect(await client.agents.listRules({agentName: "native-agent"})).toEqual({rules: []});
    const decision = await client.checks.check({stage: "pre", step: {name: "lookup", type: "tool", input: {}}});
    expect(traffic.map(({path}) => path)).toEqual([
      "/api/runtime/api/v1/agents/register", "/api/runtime/api/v1/agents/native-agent/rules",
      "/api/runtime/api/v1/checks",
    ]);
    expect(traffic[0].body).toMatchObject({agent: {agent_name: "native-agent"}, conflict_mode: "strict"});
    expect(traffic[2].body).toMatchObject({agent_name: "native-agent", execution_mode: "hosted", stage: "pre"});
    expect(decision.allowed).toBe(true);
    expect(decision.checks[0]).toMatchObject({ruleId: 7, ruleName: "limits", checkId: "native-check-7"});
  });
});
