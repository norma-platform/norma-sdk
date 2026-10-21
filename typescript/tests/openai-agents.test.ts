// Modified for Norma.
import { describe, it, expect, vi } from "vitest";
import { Agent, Runner, Usage, RunContext, type Model, type AgentOutputItem, defineToolInputGuardrail, ToolGuardrailFunctionOutputFactory } from "@openai/agents";
import { normaTool, type NormaToolRecord } from "../src/openai-agents";
import { NormaClient } from "../src/client";
import { HTTPClient } from "../src/index";
import type { Decision } from "../src/public-api";

const parameters = { type: "object" as const, properties: { text: { type: "string" as const } }, required: ["text"], additionalProperties: false as const };
const safe: Decision = { allowed: true, reason: null, checks: [] };
function blocked(action: "deny" | "steer"): Decision {
  return {allowed:false,reason:"Choose a safe message",checks:[{ruleId:1,ruleName:"safe message",action,status:"matched",result:{matched:true,confidence:1}}]};
}
function client() { const c = new NormaClient(); c.init({agentName:"norma-agent",runtimeUrl:"https://runtime.example",agentKey:"synthetic"}); return c; }
function scripted(calls: AgentOutputItem[]): Model {
  let turn=0;
  const next = () => ({usage:new Usage(),output:turn++ === 0 ? calls : [{type:"message" as const,role:"assistant" as const,status:"completed" as const,content:[{type:"output_text" as const,text:"done"}]}]});
  return {async getResponse(){return next();},async *getStreamedResponse(){yield {type:"response_done",response:{...next(),id:"response-test"}};}};
}
const call = (id="call-1", text="hello"): AgentOutputItem => ({type:"function_call",callId:id,name:"send",arguments:JSON.stringify({text})});
function setup(decision: Decision = safe) {
  const c=client(); const check=vi.fn().mockResolvedValue(decision); vi.spyOn(c,"checks","get").mockReturnValue({check});
  const execute=vi.fn(async()=>"sent"); const records: NormaToolRecord[]=[];
  const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute,onRecord:r=>{records.push(r);}});
  const agent=new Agent({name:"sender",tools:[t]});
  const runner=new Runner({model:scripted([call()]),tracingDisabled:true});
  return {c,check,execute,records,t,agent,runner};
}
describe("OpenAI Agents real Runner",()=>{
  it("checks before and after exactly one invocation, recording correlation",async()=>{
    const s=setup(); await s.runner.run(s.agent,"go"); expect(s.execute).toHaveBeenCalledTimes(1);
    expect(s.records.map(r=>[r.callId,r.agentName,r.stage,r.status])).toEqual([["call-1","norma-agent","pre","allowed"],["call-1","norma-agent","post","allowed"]]);
    expect(s.check.mock.calls[0][0].step.input).toEqual({args:[{text:"hello"}]});
  });
  it.each(["deny","steer"] as const)("%s prevents effects",async action=>{
    const s=setup(blocked(action));
    if(action==="deny") await expect(s.runner.run(s.agent,"go")).rejects.toThrow();
    else {const result=await s.runner.run(s.agent,"go");expect(JSON.stringify(result.history)).toContain("Choose a safe message");}
    expect(s.execute).not.toHaveBeenCalled();expect(s.records[0].status).toBe(action==="steer"?"steered":"denied");
  });
  it.each(["uninitialized","outage","malformed"])("%s fails closed",async mode=>{
    const s=setup(); if(mode==="uninitialized") expect(()=>s.c.init({agentName:"bad",runtimeUrl:"invalid",agentKey:"synthetic"})).toThrow();
    if(mode==="outage") s.check.mockRejectedValue(new Error("offline"));
    if(mode==="malformed") s.check.mockResolvedValue({allowed:true} as Decision);
    await expect(s.runner.run(s.agent,"go")).rejects.toThrow();expect(s.execute).not.toHaveBeenCalled();
  });
  it("post failure reports effects occurred and never replays",async()=>{
    const s=setup();s.check.mockResolvedValueOnce(safe).mockRejectedValue(new Error("offline"));
    await expect(s.runner.run(s.agent,"go")).rejects.toThrow();expect(s.execute).toHaveBeenCalledTimes(1);expect(s.records.at(-1)?.stage).toBe("post");
  });
  it("direct invoke cannot bypass native authorization",async()=>{
    const s=setup();await expect(s.t.invoke(new RunContext({}),'{"text":"hello"}')).rejects.toThrow(/permission/);expect(s.execute).not.toHaveBeenCalled();
  });
  it("parallel calls keep independent provenance",async()=>{
    const s=setup();const runner=new Runner({model:scripted([call("a"),call("b")]),tracingDisabled:true});
    await runner.run(s.agent,"go");expect(s.execute).toHaveBeenCalledTimes(2);expect(s.records.filter(r=>r.stage==="post").map(r=>r.callId).sort()).toEqual(["a","b"]);
  });
  it("uses trusted active-agent mapping even for shared tools", async()=>{
    const a=client(), b=client(); b.init({agentName:"second-agent",runtimeUrl:"https://runtime.example",agentKey:"second-synthetic"});
    const ca=vi.fn().mockResolvedValue(safe), cb=vi.fn().mockResolvedValue(safe);
    vi.spyOn(a,"checks","get").mockReturnValue({check:ca});vi.spyOn(b,"checks","get").mockReturnValue({check:cb});
    const clients=new Map<Agent,NormaClient>();const records:NormaToolRecord[]=[];
    const t=normaTool({name:"send",description:"Send",parameters,resolveClient:({agent})=>clients.get(agent),execute:async()=>"sent",onRecord:r=>{records.push(r);}});
    const aa=new Agent({name:"A",tools:[t]}),bb=new Agent({name:"B",tools:[t]});clients.set(aa,a);clients.set(bb,b);
    await Promise.all([new Runner({model:scripted([call()]),tracingDisabled:true}).run(aa,"go"),new Runner({model:scripted([call()]),tracingDisabled:true}).run(bb,"go")]);
    expect(ca).toHaveBeenCalledTimes(2);expect(cb).toHaveBeenCalledTimes(2);
    expect(records.filter(r=>r.stage==="pre").map(r=>[r.frameworkAgentName,r.agentName]).sort()).toEqual([["A","norma-agent"],["B","second-agent"]]);
    const unmapped=new Agent({name:"Unmapped",tools:[t]});await expect(new Runner({model:scripted([call()]),tracingDisabled:true}).run(unmapped,"go")).rejects.toThrow();
  });
  it("rejects client reinitialization while awaiting precheck",async()=>{
    const s=setup();s.check.mockImplementation(async()=>{s.c.init({agentName:"other",runtimeUrl:"https://runtime.example",agentKey:"other"});return safe;});
    await expect(s.runner.run(s.agent,"go")).rejects.toThrow();expect(s.execute).not.toHaveBeenCalled();
  });
  it("retains existing input guardrails",async()=>{
    const c=client();const check=vi.fn().mockResolvedValue(safe);vi.spyOn(c,"checks","get").mockReturnValue({check});const execute=vi.fn(async()=>"sent");
    const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute,inputGuardrails:[defineToolInputGuardrail({name:"existing",run:async()=>ToolGuardrailFunctionOutputFactory.throwException()})]});
    await expect(new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go")).rejects.toThrow();expect(execute).not.toHaveBeenCalled();
  });
  it("does not check or execute before human approval; resumes with fresh checks",async()=>{
    const s=setup();s.t.needsApproval=async()=>true;
    const paused=await s.runner.run(s.agent,"go");expect(paused.interruptions).toHaveLength(1);expect(s.check).not.toHaveBeenCalled();
    paused.state.approve(paused.interruptions[0]);await s.runner.run(s.agent,paused.state);
    expect(s.execute).toHaveBeenCalledTimes(1);expect(s.check).toHaveBeenCalledTimes(2);
  });
  it("cancellation during the awaited check prevents later effects",async()=>{
    const s=setup();const abort=new AbortController();s.check.mockImplementation(async()=>{abort.abort();return safe;});
    try {await s.runner.run(s.agent,"go",{signal:abort.signal});} catch { /* SDK may reject cancellation. */ }
    expect(s.execute).not.toHaveBeenCalled();
  });
  it("official Runner deduplicates repeated identical calls without repeated effects",async()=>{
    const s=setup();await new Runner({model:scripted([call("duplicate"),call("duplicate")]),tracingDisabled:true}).run(s.agent,"go");expect(s.execute).toHaveBeenCalledTimes(1);
  });
  it.each(["deny", "steer"] as const)("wire %s retains semantics when optional check ID is omitted",async action=>{
    const c=new NormaClient();c.init({agentName:"transport",runtimeUrl:"https://runtime.example",agentKey:"synthetic",observabilityEnabled:false,httpClient:new HTTPClient({fetcher:async()=>new Response(JSON.stringify({execution_contract:"hosted-v1",allowed:false,confidence:1,matches:[{rule_id:7,rule_name:"fixture",action,result:{matched:true,confidence:1,message:"Revise arguments"}}]}),{headers:{"content-type":"application/json"}})})});
    const execute=vi.fn(async()=>"sent");const records:NormaToolRecord[]=[];const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute,onRecord:r=>{records.push(r);}});
    const run=new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go");
    if(action==="deny")await expect(run).rejects.toThrow();else await run;
    expect(execute).not.toHaveBeenCalled();expect(records[0].status).toBe(action==="deny"?"denied":"steered");expect(records[0].decision?.checks[0].checkId).toBeUndefined();
  });
  it.each(["missing-proof", "bad-json", "outage", "503", "409"])("real Norma transport fails closed on %s", async mode=>{
    const fetcher=vi.fn(async()=>{
      if(mode==="outage") throw new TypeError("offline");
      return new Response(mode==="bad-json"?"not JSON":JSON.stringify({allowed:true,confidence:1}),{status:mode==="503"?503:mode==="409"?409:200,headers:{"content-type":"application/json"}});
    });
    const c=new NormaClient();c.init({agentName:"transport",runtimeUrl:"https://runtime.example",agentKey:"synthetic",observabilityEnabled:false,httpClient:new HTTPClient({fetcher})});
    const execute=vi.fn(async()=>"sent");const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute});
    await expect(new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go")).rejects.toThrow();expect(execute).not.toHaveBeenCalled();expect(fetcher).toHaveBeenCalledTimes(1);
  });
  it("rejects a client changed by the awaited record callback",async()=>{
    const c=client();vi.spyOn(c,"checks","get").mockReturnValue({check:vi.fn().mockResolvedValue(safe)});const execute=vi.fn(async()=>"sent");
    const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute,onRecord:r=>{if(r.status==="allowed")c.init({agentName:"other",runtimeUrl:"https://runtime.example",agentKey:"other"});}});
    await expect(new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go")).rejects.toThrow();expect(execute).not.toHaveBeenCalled();
  });
  it("post recording cannot rebind the client or imply a verified completion",async()=>{
    const c=client();vi.spyOn(c,"checks","get").mockReturnValue({check:vi.fn().mockResolvedValue(safe)});const execute=vi.fn(async()=>"sent");const records:NormaToolRecord[]=[];
    const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute,onRecord:r=>{records.push(r);if(r.stage==="post"&&r.status==="allowed")c.init({agentName:"other",runtimeUrl:"https://runtime.example",agentKey:"other"});}});
    await expect(new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go")).rejects.toThrow();expect(execute).toHaveBeenCalledTimes(1);expect(records.at(-1)).toMatchObject({stage:"post",status:"error"});
  });
  it("execution mutates a detached copy without changing pre/post evidence",async()=>{
    const c=client();const check=vi.fn().mockResolvedValue(safe);vi.spyOn(c,"checks","get").mockReturnValue({check});
    const t=normaTool({name:"send",description:"Send",parameters,resolveClient:()=>c,execute:async args=>{args.text="changed";return "sent";}});
    await new Runner({model:scripted([call()]),tracingDisabled:true}).run(new Agent({name:"A",tools:[t]}),"go");
    expect(check.mock.calls[0][0].step.input).toEqual({args:[{text:"hello"}]});expect(check.mock.calls[1][0].step.input).toEqual({args:[{text:"hello"}]});
    expect(Object.isFrozen(check.mock.calls[0][0].step.input.args[0])).toBe(true);
  });
  it("rejects transforming schemas explicitly",async()=>{
    const {z}=await import("zod");
    expect(()=>normaTool({name:"send",description:"Send",parameters:z.object({text:z.string().transform(v=>v.toUpperCase())}) as never,resolveClient:()=>client(),execute:async()=>"sent"})).toThrow(/plain strict JSON/);
  });
  it("streaming uses the same awaited enforcement",async()=>{
    const s=setup(blocked("deny"));const result=await s.runner.run(s.agent,"go",{stream:true});
    await expect(result.completed).rejects.toThrow();expect(s.execute).not.toHaveBeenCalled();
  });
});
