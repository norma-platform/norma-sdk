import { configure } from "norma-sdk";
import { randomUUID } from "node:crypto";

interface ChatRequest {
  session_id: string;
  turn_id: string;
  customer_id: string;
  message: string;
}

interface Invocation { args: unknown[]; agent_name: string; }
interface VerdictPayload { verdict: { verdict: string }; agent_name: string; }

async function sendMessage(recipient: string, message: string): Promise<string> {
  // Replace with the application's existing async message service.
  return `sent to ${recipient}: ${message}`;
}

const tools = { send_message: sendMessage };
const callbacks = {
  requestContext(invocation: Invocation) {
    const request = invocation.args[0] as ChatRequest;
    return { request_id: request.turn_id, customer_id: request.customer_id };
  },
  recordVerdict(payload: VerdictPayload) {
    console.info(`${payload.agent_name} finish verdict: ${payload.verdict.verdict}`);
  },
};

const application = {
  async support_chat(request: ChatRequest): Promise<string> {
    return tools.send_message("support-queue", request.message);
  },
  async billing_chat(request: ChatRequest): Promise<string> {
    return tools.send_message("billing-queue", request.message);
  },
};

async function main(): Promise<void> {
  const configured = await configure("norma-agents", {
    bindings: { application, tools, callbacks },
  });
  try {
    const common = { session_id: randomUUID(), customer_id: "customer-456" };
    console.log(await application.support_chat({ ...common, turn_id: randomUUID(), message: "How can I help?" }));
    console.log(await application.billing_chat({ ...common, turn_id: randomUUID(), message: "I have a billing question." }));
  } finally {
    const receipts = await configured.close();
    for (const [agentName, receipt] of Object.entries(receipts)) {
      console.log(agentName, receipt.status);
    }
  }
}

void main();
