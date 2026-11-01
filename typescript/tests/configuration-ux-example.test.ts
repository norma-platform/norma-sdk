// Modified for Norma.
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { configure } from "../src/instrumentation";
import { HTTPClient } from "../src/generated/lib/http";

interface CheckBody {
  agent_name?: string;
  stage?: string;
  step?: { type?: string; input?: Record<string, unknown> };
}

const agentNames = ["sdk-agent-primary", "sdk-agent-peer"] as const;
const exampleDirectory = fileURLToPath(new URL("../../examples/instrumentation/configuration-ux/typescript/norma-agents/", import.meta.url));

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } });
}

async function acknowledgeControlContract(request: Request, result: Response): Promise<Response> {
  const contract = request.headers.get("X-Norma-Control-Contract");
  if (contract !== "rule-controls-v1" || !result.ok) return result;
  const payload = await result.clone().json() as Record<string, unknown>;
  const headers = new Headers(result.headers);
  headers.set("X-Norma-Control-Contract", "rule-controls-v1");
  return new Response(JSON.stringify({ ...payload, control_contract: "rule-controls-v1" }), {
    status: result.status,
    headers,
  });
}

describe("configuration UX example", () => {
  it("loads JSON and YAML in one public configure call and records existing turn fields", async () => {
    const checkBodies: Array<{ agentName: string; body: CheckBody; contract: string | null }> = [];
    const httpClient = new HTTPClient({ fetcher: async request => {
      const url = new URL(request.url);
      const identity = agentNames.find(name => url.pathname.endsWith(`/agents/${name}`) || url.pathname.endsWith(`/agents/${name}/rules`));
      if (request.method === "GET" && identity && url.pathname.endsWith(`/agents/${identity}`)) {
        return response({ agent: { agent_name: identity }, steps: [] });
      }
      if (request.method === "GET" && identity && url.pathname.endsWith(`/agents/${identity}/rules`)) {
        return acknowledgeControlContract(request, response({ rules: [] }));
      }
      if (request.method === "POST" && url.pathname.endsWith("/api/v1/checks")) {
        const body = await request.json() as CheckBody;
        const agentName = body.agent_name ?? "unknown";
        checkBodies.push({ agentName, body, contract: request.headers.get("X-Norma-Control-Contract") });
        return acknowledgeControlContract(request, response({
          execution_contract: "hosted-v1",
          verdict: "pass",
          reason: "Complete",
          reason_codes: [],
          held_by: [],
          would_hold_by: [],
          continuation: { count: 0, max: 0 },
          allow_finish: true,
          outcome: "completed",
          provider_input_tokens: 0,
        }));
      }
      return new Response("not found", { status: 404 });
    } });

    const application = {
      async primaryChat(request: { prompt: string }) { return `Primary reply: ${request.prompt}`; },
      async *peerChat(request: { prompt: string }) {
        yield `Peer reply: ${request.prompt}`;
        yield " Stream complete.";
      },
    };
    const callbacks = {
      requestContext({ args }: { args: unknown[] }) {
        return { request_id: (args[0] as { turn_id: string }).turn_id };
      },
      recordVerdict() {},
    };
    const connections = Object.fromEntries(agentNames.map(agentName => [agentName, {
      runtimeUrl: "http://localhost:8080/api/runtime",
      agentKey: `synthetic-${agentName}`,
      observabilityEnabled: false,
      httpClient,
    }]));

    const configured = await configure(exampleDirectory, {
      bindings: { application, callbacks },
      connections,
    });
    try {
      const common = {
        session_id: "session-ux",
        trace_id: "0123456789abcdef0123456789abcdef",
        span_id: "0123456789abcdef",
      };
      const primaryRequest = { ...common, turn_id: "turn-primary", prompt: "primary" };
      const peerRequest = { ...common, turn_id: "turn-peer", prompt: "peer" };
      await expect(application.primaryChat(primaryRequest)).resolves.toBe("Primary reply: primary");
      const chunks: string[] = [];
      for await (const chunk of application.peerChat(peerRequest)) chunks.push(chunk);
      expect(chunks).toEqual(["Peer reply: peer", " Stream complete."]);

      const finishRequests = checkBodies.filter(({ body }) => body.step?.type === "turn");
      expect(finishRequests).toHaveLength(2);
      expect(finishRequests.map(({ agentName }) => agentName)).toEqual(agentNames);
      expect(finishRequests.map(({ contract }) => contract)).toEqual(["rule-controls-v1", "rule-controls-v1"]);
      expect(finishRequests[0]?.body.step?.input).toMatchObject({
        session_id: common.session_id,
        platform: "typescript-local-omlx-trial",
        platform_version: "configuration-parity",
        user_request: "primary",
      });
      expect(finishRequests[1]?.body.step?.input).toMatchObject({
        session_id: common.session_id,
        platform: "typescript-local-omlx-trial",
        platform_version: "configuration-parity",
        user_request: "peer",
        final_message: "Peer reply: peer Stream complete.",
      });
    } finally {
      await configured.close();
    }
  });

  it("keeps explicit parameter-name examples separate from the bootstrap directory", () => {
    const optionalExample = readFileSync(new URL("../../examples/instrumentation/configuration-ux/typescript/parameter-names.example.yaml", import.meta.url), "utf8");
    expect(optionalExample).toContain("parameter_names: [request, options]");
    expect(optionalExample).toContain("path: [params, request, session_id]");
  });
});
