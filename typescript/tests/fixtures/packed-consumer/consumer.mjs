// Modified for Norma.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import norma, { NormaClient, HTTPClient, checkpoint, RuleViolationError, CheckError } from "norma-sdk";
const receipt = JSON.parse(await readFile(new URL("./receipt.json", import.meta.url), "utf8"));
const requests=[];
const checkBodies=[];
let deny=false;
let postFailure=false;
globalThis.fetch=()=>{throw new Error("Network forbidden in offline qualification");};
const transport=new HTTPClient({fetcher:async request=>{
 requests.push(request);
 assert.equal(request.redirect,"error");
 assert.equal(request.headers.get("x-api-key"),"synthetic-key");
 assert.ok(request.url.startsWith("https://tenant.invalid/api/runtime/"));
 const body=request.method==="GET"?null:await request.clone().json();
 let result=receipt;
 if(request.url.endsWith("/checks")) {
   checkBodies.push(body);
   if(postFailure && body.stage==="post") return new Response("unavailable",{status:503});
   result={execution_contract:"hosted-v1",allowed:!deny,confidence:1,matches:deny?[{rule_id:1,rule_name:"Blocked",action:"deny",result:{confidence:1,matched:true}}]:[],non_matches:[],errors:[]};
 }
 return new Response(JSON.stringify(result),{headers:{"content-type":"application/json"}});
}});
norma.init({agentName:receipt.agent_name,runtimeUrl:"https://tenant.invalid/api/runtime",agentKey:"synthetic-key",httpClient:transport,observabilityEnabled:false});
assert.ok(norma instanceof NormaClient);
const step={type:"tool",name:receipt.request.step.name,input:receipt.request.step.input};
assert.equal((await norma.checks.check({stage:"pre",step})).allowed,true);
const record=await norma.operations.create({operationKey:receipt.operation_key,step});
assert.equal(record.id,receipt.id);
assert.equal((await norma.operations.get(record.id)).id,record.id);
let calls=0;
const action=checkpoint(async value=>{calls++;return value;});
deny=true;
assert.equal((await norma.checks.check({stage:"pre",step})).allowed,false);
await assert.rejects(action("blocked"),RuleViolationError);
assert.equal(calls,0);
deny=false;
assert.equal(await action("allowed"),"allowed");
assert.equal(calls,1);
postFailure=true;
await assert.rejects(action("ran once"),CheckError);
assert.equal(calls,2);
postFailure=false;
let captures=0;
const source={available:7};
const observed=checkpoint(async value=>{source.available=0;return value;},{
 state:()=>{captures++;return {inventory:source};},
});
assert.equal(await observed("observed"),"observed");
assert.equal(captures,1);
assert.deepEqual(checkBodies.slice(-2).map(body=>body.step.context.state),[
 {inventory:{available:7}},{inventory:{available:7}},
]);
assert.deepEqual(checkBodies.slice(-2).map(body=>body.step.context.norma_argument_shape),[
 "positional","positional",
]);
const noSerialize=checkpoint(async()=>{throw new Error("must not run");},{
 state:{get hidden(){throw new Error("must not read");}},
});
await assert.rejects(noSerialize(),CheckError);
for(const group of ["auth","system","rules","policies","rules"]) assert.equal(group in norma,false);
console.log("Packed hosted ESM checks, receipts and action safety passed.");

// Core import remains usable without the optional OpenAI peer.
await assert.rejects(import("norma-sdk/openai-agents"), error => error.code === "ERR_MODULE_NOT_FOUND" && error.message.includes("@openai/agents"));
