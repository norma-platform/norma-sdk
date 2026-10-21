// Modified for Norma.
import { describe, expect, it, vi } from "vitest";
import { NormaClient, HTTPClient, checkpoint, CheckError, checkFinish, TurnRecorder } from "../src/index";
const json=(body:unknown)=>new Response(JSON.stringify(body),{headers:{"content-type":"application/json"}});
function client(fetcher:(request:Request)=>Promise<Response>) {
 const sdk=new NormaClient(); sdk.init({agentName:"hosted-agent",agentKey:"synthetic-key",runtimeUrl:"https://tenant.example/api/runtime",observabilityEnabled:false,httpClient:new HTTPClient({fetcher})});return sdk;
}
describe("atomic hosted execution proof",()=>{
 it.each([undefined,"unknown-v2"])("rejects missing or unknown proof %s before effects",async proof=>{
  const requests:Request[]=[];
  const sdk=client(async request=>{requests.push(request);return json({execution_contract:proof,allowed:true,confidence:1});});
  const effect=vi.fn(async()=>"done");
  await expect(checkpoint(effect,{client:sdk})()).rejects.toBeInstanceOf(CheckError);
  expect(effect).not.toHaveBeenCalled();
  expect(await requests[0].json()).toMatchObject({execution_mode:"hosted"});
 });
 it("requires fresh proof after execution and never replays",async()=>{
  let checks=0;
  const sdk=client(async()=>json({execution_contract:++checks===1?"hosted-v1":undefined,allowed:true,confidence:1}));
  const effect=vi.fn(async()=>"done");
  await expect(checkpoint(effect,{client:sdk})()).rejects.toMatchObject({stage:"post"});
  expect(effect).toHaveBeenCalledTimes(1);expect(checks).toBe(2);
 });
 it.each(["X-Tenant-Id","X-User-Id","X-Account-Id","X-API-Key","Authorization","Cookie"])("rejects identity override %s before transmission",async header=>{
  const fetcher=vi.fn(async()=>json({agents:[]}));const sdk=client(fetcher);
  await expect(sdk.agents.list({}, {headers:{[header]:"synthetic"}})).rejects.toThrow();
  expect(fetcher).not.toHaveBeenCalled();
 });
 it("does not treat a finish without proof as verified",async()=>{
  const fetcher=vi.spyOn(globalThis,"fetch").mockResolvedValue(json({verdict:"pass",allow_finish:true,outcome:"completed",continuation:{count:0,max:0},reason:"ok",rule_ids:[],check_ids:[],blocking_checks:[]}));
  try {
   const result=await checkFinish(new TurnRecorder({sessionId:"s",turnId:"t"}).turn("Done"),{client:client(async()=>json({}))});
   expect(result).toMatchObject({ verdict: "unavailable", allow_finish: true, outcome: "finished_unverified" });
   expect(result.verdict).not.toBe("pass");
  } finally {fetcher.mockRestore();}
 });
});

describe("credential diagnostics",()=>{
 it("omits credentials from public config, serialization and debug logs",async()=>{
  const messages:unknown[]=[];
  const logger={group:(value:unknown)=>messages.push(value),groupEnd:()=>{},log:(...values:unknown[])=>messages.push(values)};
  const sdk=new NormaClient();sdk.init({agentName:"hosted-agent",agentKey:"synthetic-secret",runtimeUrl:"https://tenant.example",debugLogger:logger,httpClient:new HTTPClient({fetcher:async()=>json({agents:[],pagination:{has_more:false,limit:20}})})});
  await sdk.agents.list();
  expect(JSON.stringify([sdk,sdk.config,messages])).not.toContain("synthetic-secret");
 });
 it("rejects unsupported identity and auth-mode settings",()=>{
  const sdk=new NormaClient();
  // @ts-expect-error Unknown settings are rejected for untyped callers too.
  expect(()=>sdk.init({agentName:"hosted-agent",runtimeUrl:"https://tenant.example",agentKey:"synthetic",tenantId:"other"})).toThrow(/Unsupported/);
 });
 it("rejects an empty dynamic credential before transmission",async()=>{
  const fetcher=vi.fn(async()=>json({agents:[]}));const sdk=new NormaClient();
  sdk.init({agentName:"hosted-agent",runtimeUrl:"https://tenant.example",agentKey:async()=>"",httpClient:new HTTPClient({fetcher})});
  await expect(sdk.agents.list()).rejects.toThrow(/empty key/);expect(fetcher).not.toHaveBeenCalled();
 });
});

describe("hosted checkpoint transport lifecycle", () => {
  it.each([401, 403, 409, 422, 503])("blocks effects on HTTP %s", async (status) => {
    // Given: a hosted transport refuses or cannot evaluate the proposed action.
    const fetcher = vi.fn(async () => new Response("{}", {
      status, headers: { "content-type": "application/json" },
    }));
    const effect = vi.fn(async () => "done");
    // When: invoking the public checkpoint.
    await expect(checkpoint(effect, { client: client(fetcher) })()).rejects.toBeInstanceOf(CheckError);
    // Then: the action never runs and the returned decision is not retried.
    expect(effect).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("blocks effects on a disconnected transport", async () => {
    // Given: the connection fails before any decision is available.
    const fetcher = vi.fn(async () => { throw new TypeError("synthetic network failure"); });
    const effect = vi.fn(async () => "done");
    // When: invoking the public checkpoint.
    await expect(checkpoint(effect, { client: client(fetcher) })()).rejects.toBeInstanceOf(CheckError);
    // Then: no effect can be inferred from the failed request.
    expect(effect).not.toHaveBeenCalled();
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it("does not replay an effect after postcheck transport failure", async () => {
    // Given: precheck succeeds but postcheck cannot reach the runtime.
    const fetcher = vi.fn()
      .mockResolvedValueOnce(json({ execution_contract: "hosted-v1", allowed: true, confidence: 1 }))
      .mockRejectedValueOnce(new TypeError("synthetic network failure"));
    const effect = vi.fn(async () => "done");
    // When: invoking the public checkpoint.
    await expect(checkpoint(effect, { client: client(fetcher) })()).rejects.toMatchObject({ stage: "post" });
    // Then: the effect happened once; the caller must reconcile it before retrying.
    expect(effect).toHaveBeenCalledTimes(1);
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
});
