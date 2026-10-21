/** Illustrative setup only; no inference or credential creation occurs on import. */
import { Agent } from "@openai/agents";
import { NormaClient } from "norma-sdk";
import { normaTool } from "norma-sdk/openai-agents";

export function createSupportAgent() {
  const client = new NormaClient();
  client.init({ agentName: "support-agent" });
  const clients = new Map<Agent, NormaClient>();
  const lookup = normaTool({
    name: "lookup_order", description: "Look up an order",
    parameters: { type: "object", properties: { orderId: { type: "string" } }, required: ["orderId"], additionalProperties: false },
    resolveClient: ({ agent }) => clients.get(agent),
    execute: async ({ orderId }, _context, signal) => {
      signal?.throwIfAborted();
      return { orderId, status: "example" };
    },
  });
  const agent = new Agent({ name: "Support", tools: [lookup] });
  clients.set(agent, client);
  return agent;
}
