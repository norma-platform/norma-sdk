// Modified for Norma.
import { afterEach, describe, expect, it, vi } from "vitest";
import norma, { NormaClient, HTTPClient } from "../src/index";
afterEach(() => vi.unstubAllEnvs());
describe("hosted configuration", () => {
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
  it("does not expose management groups", () => {
    const client=new NormaClient();
    for (const key of ["auth","system","policies","rules","rules"]) expect(client).not.toHaveProperty(key);
  });
});
