// Modified for Norma.
import assert from 'node:assert/strict';
import { Agent, Runner, Usage } from '@openai/agents';
import { NormaClient, HTTPClient } from 'norma-sdk';
import { normaTool } from 'norma-sdk/openai-agents';
const schema = {type:'object',properties:{text:{type:'string'}},required:['text'],additionalProperties:false};
for (const mode of ['allow','deny','steer','reinitialize','outage']) {
  let effects=0, turn=0;
  const records=[];
  const c=new NormaClient();
  c.init({agentName:'installed-agent',runtimeUrl:'https://runtime.example',agentKey:'synthetic',observabilityEnabled:false,
    httpClient:new HTTPClient({fetcher:async()=>{
      if(mode==='outage')throw new TypeError('offline');
      return new Response(JSON.stringify({execution_contract:'hosted-v1',confidence:1,allowed:mode!=='deny'&&mode!=='steer',
        matches:mode==='deny'||mode==='steer'?[{rule_id:1,rule_name:'fixture',action:mode,result:{matched:true,confidence:1,message:'Revise arguments'}}]:[],non_matches:[],errors:[]}),{headers:{'content-type':'application/json'}});
    }})});
  const clients=new Map();
  const t=normaTool({name:'send',description:'Send',parameters:schema,resolveClient:({agent})=>clients.get(agent),
    execute:async()=>{effects++;return 'sent';},onRecord:r=>{records.push(r);if(mode==='reinitialize'&&r.status==='allowed')c.init({agentName:'other',runtimeUrl:'https://runtime.example',agentKey:'other'});}});
  const agent=new Agent({name:'Installed',tools:[t]});clients.set(agent,c);
  const model={async getResponse(){return {usage:new Usage(),output:turn++===0?[{type:'function_call',name:'send',callId:'installed-call',arguments:'{"text":"hello"}'}]:[{type:'message',role:'assistant',status:'completed',content:[{type:'output_text',text:'done'}]}]};},async *getStreamedResponse(){throw new Error('unused');}};
  const execution=new Runner({model,tracingDisabled:true}).run(agent,'go');
  if(['deny','outage','reinitialize'].includes(mode))await assert.rejects(execution);else await execution;
  assert.equal(effects,mode==='allow'?1:0);
  assert.ok(records.every(r=>r.callId==='installed-call'&&r.frameworkAgentName==='Installed'));
}
console.log(JSON.stringify({status:'passed',cases:['allow','deny','steer','reinitialize','outage'],runner:'@openai/agents@0.20.0'}));
