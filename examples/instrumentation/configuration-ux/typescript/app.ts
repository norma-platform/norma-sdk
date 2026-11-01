import { randomUUID } from "node:crypto";
import { configure } from "norma-sdk";

interface ChatRequest {
  session_id: string;
  turn_id: string;
  trace_id: string;
  span_id: string;
  prompt: string;
}

interface Invocation { args: unknown[]; }
interface VerdictPayload { verdict: { verdict: string }; agent_name: string; }

export const application = {
  async primaryChat(request: ChatRequest): Promise<string> {
    return `Primary reply: ${request.prompt}`;
  },
  async *peerChat(request: ChatRequest): AsyncGenerator<string> {
    yield `Peer reply: ${request.prompt}`;
    yield " Stream complete.";
  },
};

export const callbacks = {
  requestContext(invocation: Invocation) {
    const request = invocation.args[0] as ChatRequest;
    return { request_id: request.turn_id };
  },
  recordVerdict(payload: VerdictPayload) {
    console.info(`${payload.agent_name} finish verdict: ${payload.verdict.verdict}`);
  },
};

async function main(): Promise<void> {
  const configured = await configure("norma-agents", {
    bindings: { application, callbacks },
  });
  try {
    const common = {
      session_id: randomUUID(),
      trace_id: "0123456789abcdef0123456789abcdef",
      span_id: "0123456789abcdef",
    };
    console.log(await application.primaryChat({
      ...common,
      turn_id: randomUUID(),
      prompt: "Reply from the primary identity.",
    }));
    for await (const delta of application.peerChat({
      ...common,
      turn_id: randomUUID(),
      prompt: "Reply from the peer identity.",
    })) {
      process.stdout.write(delta);
    }
    process.stdout.write("\n");
  } finally {
    const receipts = await configured.close();
    for (const [agentName, receipt] of Object.entries(receipts)) {
      console.log(agentName, receipt.status);
    }
  }
}

void main();
