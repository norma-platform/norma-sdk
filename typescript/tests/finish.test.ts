// Modified for Norma.
import { checkFor } from "../src/client";
import { afterEach, describe, expect, it, vi } from "vitest";

import { NormaClient } from "../src/client";
import { checkpoint } from "../src/checkpoint";
import { checkFinish, TurnRecorder } from "../src/finish";

function client(): NormaClient {
  const active = new NormaClient();
  active.init({ agentName: "coding-agent",  runtimeUrl: "http://localhost:8000", agentKey: "test-key" });
  return active;
}

describe("TurnRecorder", () => {
  it("keeps the most recent 50 actions, redacts credentials, and tracks successful edits", () => {
    const recorder = new TurnRecorder({ sessionId: "session-1", turnId: "turn-1", userRequest: "Use Bearer abc.def" });
    for (let index = 0; index < 51; index++) recorder.recordAction({
      kind: "edit", nativeTool: "file.write", status: "succeeded", target: `/repo/file-${index}`,
      outputTail: `MY_SECRET=super-secret-${index}`,
    });
    const turn = recorder.turn("Done. Bearer abc.def");
    expect(turn.actions).toHaveLength(50);
    expect(turn.actions[0]?.index).toBe(1);
    expect(turn.actions_truncated).toBe(true);
    expect(turn.actions[0]?.output_tail).toBe("MY_SECRET=[REDACTED]");
    expect(turn.user_request).toBe("Use Bearer [REDACTED]");
    expect(turn.final_message).toBe("Done. Bearer [REDACTED]");
    expect(turn.files_touched).toHaveLength(51);
    expect(turn.evidence_source).toBe("sdk");
  });

  it("automatically records decorated calls within the active async turn", async () => {
    const active = client();
    vi.spyOn(checkFor(active), "check").mockResolvedValue({ confidence: 1, allowed: true, matches: [], nonMatches: [], errors: [] });
    const recorder = new TurnRecorder({ sessionId: "session-2", turnId: "turn-2" });
    const edit = checkpoint(async (path: string) => ({ exit_code: 0, path }), {
      client: active, stepName: "tools.write", action: (target: string) => ({ kind: "edit", target }),
    });
    await recorder.run(async () => edit("/repo/app.ts"));
    expect(recorder.turn("Finished").actions).toMatchObject([{
      kind: "edit", native_tool: "tools.write", status: "succeeded", target: "/repo/app.ts", exit_code: 0,
    }]);
  });

  it("marks a completed tool error as failed and keeps returned evidence snapshots immutable", async () => {
    const active = client();
    vi.spyOn(checkFor(active), "check").mockResolvedValue({ confidence: 1, allowed: true, matches: [], nonMatches: [], errors: [] });
    const recorder = new TurnRecorder({ sessionId: "s", turnId: "t", maxContinuations: 0 });
    const tool = checkpoint(async () => ({ isError: true, content: "failed" }), { client: active, stepName: "tools.exec" });
    await recorder.run(tool);
    expect(recorder.turn("Done").actions[0]?.status).toBe("failed");
    const returned = recorder.recordAction({ kind: "other", nativeTool: "native.call", status: "succeeded" });
    returned.native_tool = "tampered";
    const snapshot = recorder.turn("Done");
    snapshot.actions[0]!.native_tool = "changed";
    expect(recorder.turn("Done").actions.map((action) => action.native_tool)).toEqual(["tools.exec", "native.call"]);
    expect(snapshot.continuation).toEqual({ count: 0, max: 0 });
    expect(new TurnRecorder({ sessionId: "s", turnId: "t", maxContinuations: 3 }).turn("Done", 3).continuation.max).toBe(3);
  });

  it("isolates concurrent and nested async recorder scopes", async () => {
    const first = new TurnRecorder({ sessionId: "first", turnId: "one" });
    const second = new TurnRecorder({ sessionId: "second", turnId: "two" });
    await Promise.all([
      first.run(async () => {
        await Promise.resolve();
        first.recordAction({ kind: "other", nativeTool: "first", status: "succeeded" });
        await second.run(async () => second.recordAction({ kind: "other", nativeTool: "nested", status: "succeeded" }));
      }),
      second.run(async () => {
        await Promise.resolve();
        second.recordAction({ kind: "other", nativeTool: "second", status: "succeeded" });
      }),
    ]);
    expect(first.turn("done").actions.map((action) => action.native_tool)).toEqual(["first"]);
    expect(second.turn("done").actions.map((action) => action.native_tool).sort()).toEqual(["nested", "second"]);
  });
});

describe("checkFinish", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("sends a redacted turn through the protected check seam and returns the shared verdict", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      execution_contract: "hosted-v1", verdict: "hold", reason: "A test failed", reason_codes: ["verification_claim_unsupported"],
      held_by: [4], would_hold_by: [], continuation: { count: 0, max: 2 },
      allow_finish: false, outcome: "held", provider_input_tokens: 20,
    }), { status: 200, headers: { "content-type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const recorder = new TurnRecorder({ sessionId: "session-3", turnId: "turn-3" });
    const verdict = await checkFinish(recorder.turn("All tests pass. Bearer abc.def"), { client: client() });
    expect(verdict.verdict).toBe("hold");
    expect(verdict.allow_finish).toBe(false);
    const request = fetchMock.mock.calls[0]?.[0] as Request;
    expect(request.url).toBe("http://localhost:8000/api/v1/checks");
    expect(request.headers.get("X-API-Key")).toBe("test-key");
    const body = await request.clone().json();
    expect(body).toMatchObject({ agent_name: "coding-agent", stage: "pre", step: { type: "turn", name: "typescript-sdk.finish", input: { final_message: "All tests pass. Bearer [REDACTED]" } } });
  });

  it("allows an unverified finish when the runtime is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const recorder = new TurnRecorder({ sessionId: "session-4", turnId: "turn-4" });
    const verdict = await checkFinish(recorder.turn("Done"), { client: client() });
    expect(verdict).toMatchObject({ verdict: "unavailable", allow_finish: true, outcome: "finished_unverified" });
  });

  it("rejects a caller-mutated non-SDK turn before sending it", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const turn = new TurnRecorder({ sessionId: "s", turnId: "t" }).turn("Done");
    (turn as unknown as { evidence_source: string }).evidence_source = "self_report";
    expect((await checkFinish(turn, { client: client() })).verdict).toBe("unavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("bounds the full finish operation even when the key provider stalls", async () => {
    vi.useFakeTimers();
    try {
      const active = new NormaClient();
      active.init({ agentName: "coding-agent",  runtimeUrl: "http://localhost:8000", agentKey: () => new Promise<string>(() => {}) });
      const promise = checkFinish(new TurnRecorder({ sessionId: "s", turnId: "t" }).turn("Done"), { client: active });
      await vi.advanceTimersByTimeAsync(8_001);
      expect((await promise).verdict).toBe("unavailable");
    } finally { vi.useRealTimers(); }
  });

  it("does not send a request when a key resolves after the finish deadline", async () => {
    vi.useFakeTimers();
    try {
      let resolveKey: ((value: string) => void) | undefined;
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      const active = new NormaClient();
      active.init({ agentName: "coding-agent",  runtimeUrl: "http://localhost:8000", agentKey: () => new Promise<string>((resolve) => { resolveKey = resolve; }) });
      const promise = checkFinish(new TurnRecorder({ sessionId: "s", turnId: "t" }).turn("Done"), { client: active });
      await vi.advanceTimersByTimeAsync(8_001);
      expect((await promise).verdict).toBe("unavailable");
      resolveKey?.("late-key");
      await Promise.resolve();
      expect(fetchMock).not.toHaveBeenCalled();
    } finally { vi.useRealTimers(); }
  });

  it("records a completed action once even when post check denies", async () => {
    const active = client();
    vi.spyOn(checkFor(active), "check")
      .mockResolvedValueOnce({ confidence: 1, allowed: true, matches: [], nonMatches: [], errors: [] })
      .mockResolvedValueOnce({ confidence: 1, allowed: false, matches: [{
        action: "deny", ruleId: 2, ruleName: "post-check",
        result: { confidence: 1, matched: true, message: "The result is unsafe" },
      }], nonMatches: [], errors: [] });
    const recorder = new TurnRecorder({ sessionId: "s", turnId: "t" });
    const work = vi.fn(async () => "completed work");
    await expect(recorder.run(() => checkpoint(work, { client: active, stepName: "tools.run" })())).rejects.toThrow("post-check");
    expect(work).toHaveBeenCalledTimes(1);
    expect(recorder.turn("Done").actions).toMatchObject([{ native_tool: "tools.run", status: "succeeded" }]);
  });

  it("rejects an oversized multi-byte request before making a network call", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const recorder = new TurnRecorder({ sessionId: "s", turnId: "t" });
    for (let index = 0; index < 50; index++) recorder.recordAction({
      kind: "shell", nativeTool: "🔒".repeat(200), status: "succeeded",
      target: "界".repeat(300), command: "界".repeat(500), outputTail: "界".repeat(1000),
    });
    expect((await checkFinish(recorder.turn("Done"), { client: client() })).verdict).toBe("unavailable");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects oversized or inconsistent responses without replaying the finish", async () => {
    const fetchMock = vi.fn(async () => new Response("x".repeat(300_000), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const turn = new TurnRecorder({ sessionId: "s", turnId: "t", maxContinuations: 3 }).turn("Done", 3);
    expect((await checkFinish(turn, { client: client() })).verdict).toBe("unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not accept a server verdict that elevates the host continuation limit", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({
      execution_contract: "hosted-v1", verdict: "hold", reason: "Continue", reason_codes: [], held_by: [4], would_hold_by: [],
      continuation: { count: 0, max: 3 }, allow_finish: false, outcome: "held", provider_input_tokens: null,
    }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const turn = new TurnRecorder({ sessionId: "s", turnId: "t", maxContinuations: 2 }).turn("Done");
    expect((await checkFinish(turn, { client: client() })).verdict).toBe("unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
