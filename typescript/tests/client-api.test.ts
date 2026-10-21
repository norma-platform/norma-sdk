// Modified for Norma.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { NormaClient } from "../src/client";

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "content-type": "application/json",
    },
  });
}

describe("NormaClient API wiring", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("throws when endpoint groups are accessed before init", () => {
    const client = new NormaClient();

    expect(() => client.agents).toThrow(/not initialized/i);
  });

  it("builds GET requests with query params and X-API-Key auth", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        agents: [],
        pagination: {
          has_more: false,
          limit: 5,
          next_cursor: null,
          total: 0,
        },
      }),
    );

    const client = new NormaClient();
    client.init({
      agentName: "test-agent",
      runtimeUrl: "https://api.example.com",
      agentKey: "test-api-key",
    });

    await client.agents.list({
      limit: 5,
      name: "support",
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls[0]?.[0] as Request;

    expect(request.method).toBe("GET");
    expect(request.url).toBe("https://api.example.com/api/v1/agents?limit=5&name=support");
    expect(request.headers.get("X-API-Key")).toBe("test-api-key");
  });

  it("passes debugLogger through to the generated SDK", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        agents: [],
        pagination: {
          has_more: false,
          limit: 20,
          next_cursor: null,
          total: 0,
        },
      }),
    );

    const debugLogger = {
      group: vi.fn(),
      groupEnd: vi.fn(),
      log: vi.fn(),
    };

    const client = new NormaClient();
    client.init({
      agentName: "test-agent", agentKey: "synthetic-test-key",
      runtimeUrl: "https://api.example.com",
      debugLogger,
    });

    await client.agents.list();

    expect(debugLogger.group).toHaveBeenCalledWith(
      "> Request: GET https://api.example.com/api/v1/agents",
    );
    expect(debugLogger.group).toHaveBeenCalledWith(
      "< Response: GET https://api.example.com/api/v1/agents",
    );
    expect(debugLogger.log).toHaveBeenCalledWith("Status Code:", 200, "");
  });


  it("defaults register conflict_mode to strict", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        created: true,
      }),
    );

    const client = new NormaClient();
    client.init({
      agentName: "test-agent", agentKey: "synthetic-test-key",
      runtimeUrl: "https://api.example.com",
    });

    await client.agents.register({
      agent: {
        agentId: "550e8400-e29b-41d4-a716-446655440000",
        agentName: "test-agent",
      },
    });

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    await expect(request.clone().json()).resolves.toMatchObject({
      conflict_mode: "strict",
    });
  });
});
