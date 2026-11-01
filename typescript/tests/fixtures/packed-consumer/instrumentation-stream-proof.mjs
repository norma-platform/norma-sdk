import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

export async function runStreamProof({ HTTPClient, configure, FinishHeldError }) {
  const agentName = "packed-stream-agent";
  const directory = await mkdtemp(join(tmpdir(), "norma-packed-stream-"));
  const finishRequests = [];
  const connection = name => ({
    runtimeUrl: `http://localhost:8090/${name}`,
    agentKey: `key-${name}`,
    observabilityEnabled: false,
    httpClient: new HTTPClient({ fetcher: async request => {
      const url = new URL(request.url);
      const path = url.pathname.replace(`/${name}`, "");
      if (path === `/api/v1/agents/${name}`) return new Response(JSON.stringify({ agent: { agent_name: name }, steps: [] }), { headers: { "content-type": "application/json" } });
      if (path === `/api/v1/agents/${name}/rules`) return new Response(JSON.stringify({ rules: [] }), { headers: { "content-type": "application/json" } });
      if (path === "/api/v1/checks") {
        const body = JSON.parse(await request.text());
        if (body.step?.type === "turn") {
          finishRequests.push(body);
          const held = body.step.input.turn_id === "hold-turn";
          return new Response(JSON.stringify({
            execution_contract: "hosted-v1", verdict: held ? "hold" : "pass",
            reason: held ? "Review required" : "Complete", reason_codes: [],
            held_by: held ? [7] : [], would_hold_by: [],
            continuation: { count: 0, max: held ? 1 : 0 },
            allow_finish: !held, outcome: held ? "held" : "completed", provider_input_tokens: 0,
          }), { headers: { "content-type": "application/json" } });
        }
        return new Response(JSON.stringify({ confidence: 1, allowed: true, matches: [], non_matches: [], errors: [], execution_contract: "hosted-v1" }), { headers: { "content-type": "application/json" } });
      }
      return new Response("not found", { status: 404 });
    } }),
  });

  let structuredFactoryCalls = 0;
  let promiseFactoryCalls = 0;
  let unopenedFactoryCalls = 0;
  let earlyCleanup = false;
  const app = {
    async *stringStream() {
      yield "Hello ";
      yield "world";
      return { response_id: "string-terminal" };
    },
    structuredStream() {
      structuredFactoryCalls += 1;
      return (async function* () {
        yield { text: "structured " };
        yield { text: "chunks" };
        return { response_id: "structured-terminal" };
      })();
    },
    promiseStream() {
      promiseFactoryCalls += 1;
      return Promise.resolve((async function* () {
        yield "promise ";
        yield "factory";
      })());
    },
    unopenedStream() {
      unopenedFactoryCalls += 1;
      return (async function* () {
        yield "never opened";
      })();
    },
    async *earlyStream() {
      try {
        yield "partial";
        yield "unseen";
      } finally {
        earlyCleanup = true;
      }
    },
    async *unicodeStream() {
      yield "🙂".repeat(8000);
    },
  };
  const streamTurn = (sessionId, turnId, text) => ({
    session_id: { value: sessionId },
    turn_id: { value: turnId },
    finish: { stream: text ? { text: { path: ["result", "text"] } } : {}, on_hold: "raise" },
  });
  const config = {
    version: 2,
    entrypoints: [
      { target: "app.stringStream", turn: streamTurn("session-string", "string-turn") },
      { target: "app.structuredStream", returns: "async_iterable", turn: streamTurn("session-structured", "hold-turn", true) },
      { target: "app.promiseStream", returns: "promise", turn: streamTurn("session-promise", "promise-turn") },
      { target: "app.unopenedStream", returns: "async_iterable", turn: streamTurn("session-unopened", "unopened-turn") },
      { target: "app.earlyStream", turn: streamTurn("session-early", "early-turn") },
      { target: "app.unicodeStream", turn: streamTurn("session-unicode", "unicode-turn") },
    ],
    checkpoints: {},
  };

  try {
    await writeFile(join(directory, `${agentName}.json`), JSON.stringify(config));
    const configured = await configure(directory, {
      bindings: { app },
      connections: { [agentName]: connection(agentName) },
    });
    try {
      assert.equal(structuredFactoryCalls, 0, "async iterable factory is not probed during configure");
      assert.equal(promiseFactoryCalls, 0, "promise factory is not probed during configure");
      assert.equal(unopenedFactoryCalls, 0, "unstarted AsyncIterable factory is not probed during configure");

      const unopened = app.unopenedStream("session-unopened", "unopened-turn");
      assert.equal(typeof unopened.then, "undefined");
      assert.equal(unopenedFactoryCalls, 0, "unstarted AsyncIterable factory is not called by wrapper creation");
      assert.deepEqual(await unopened.return("closed-before-first-next"), { done: true, value: "closed-before-first-next" });
      assert.equal(unopenedFactoryCalls, 0, "return before the first advance leaves the source factory unopened");
      assert.equal(finishRequests.length, 0, "return before the first advance never checks finish");

      const stringIterable = app.stringStream("session-string", "string-turn");
      assert.equal(typeof stringIterable.then, "undefined", "native async generator returns an iterable directly");
      const strings = stringIterable[Symbol.asyncIterator]();
      assert.deepEqual(await strings.next(), { done: false, value: "Hello " });
      assert.equal(finishRequests.length, 0, "finish waits for natural exhaustion");
      assert.deepEqual(await strings.next(), { done: false, value: "world" });
      assert.equal(finishRequests.length, 0, "finish does not run after a yielded chunk");
      assert.deepEqual(await strings.next(), { done: true, value: { response_id: "string-terminal" } });
      assert.equal(finishRequests.length, 1);
      assert.equal(finishRequests[0].step.input.final_message, "Hello world");
      assert.deepEqual(await strings.next(), { done: true, value: undefined });
      assert.equal(finishRequests.length, 1, "repeat exhaustion does not duplicate finish");

      const structuredIterable = app.structuredStream("session-structured", "hold-turn");
      assert.equal(typeof structuredIterable.then, "undefined", "declared AsyncIterable factory returns directly");
      assert.equal(structuredFactoryCalls, 0, "declared AsyncIterable factory stays lazy until iteration");
      const structured = structuredIterable[Symbol.asyncIterator]();
      assert.deepEqual(await structured.next(), { done: false, value: { text: "structured " } });
      assert.equal(structuredFactoryCalls, 1, "declared AsyncIterable factory runs once on first advance");
      assert.equal(finishRequests.length, 1, "structured stream has no partial finish");
      assert.deepEqual(await structured.next(), { done: false, value: { text: "chunks" } });
      assert.equal(finishRequests.length, 1, "hold check starts on terminal advance only");
      await assert.rejects(structured.next(), error => {
        assert.ok(error instanceof FinishHeldError);
        assert.equal(error.result, "structured chunks");
        assert.equal(error.verdict.verdict, "hold");
        return true;
      });
      assert.equal(finishRequests.length, 2);
      assert.equal(finishRequests[1].step.input.final_message, "structured chunks");

      const promise = app.promiseStream("session-promise", "promise-turn");
      assert.ok(promise instanceof Promise, "promise factory preserves Promise<AsyncIterable> call shape");
      assert.equal(promiseFactoryCalls, 0, "promise factory stays lazy until iteration");
      const promisedIterable = await promise;
      assert.equal(typeof promisedIterable.then, "undefined");
      const promised = promisedIterable[Symbol.asyncIterator]();
      assert.deepEqual(await promised.next(), { done: false, value: "promise " });
      assert.equal(promiseFactoryCalls, 1, "promise factory runs once on first advance");
      assert.deepEqual(await promised.next(), { done: false, value: "factory" });
      assert.deepEqual(await promised.next(), { done: true, value: undefined });
      assert.equal(finishRequests.length, 3);
      assert.equal(finishRequests[2].step.input.final_message, "promise factory");

      const beforeEarlyClose = finishRequests.length;
      const early = app.earlyStream("session-early", "early-turn")[Symbol.asyncIterator]();
      assert.deepEqual(await early.next(), { done: false, value: "partial" });
      assert.deepEqual(await early.return("caller-closed"), { done: true, value: "caller-closed" });
      assert.equal(earlyCleanup, true);
      assert.equal(finishRequests.length, beforeEarlyClose, "early return never runs finish");

      const unicodeText = "🙂".repeat(8000);
      const unicode = app.unicodeStream("session-unicode", "unicode-turn")[Symbol.asyncIterator]();
      assert.deepEqual(await unicode.next(), { done: false, value: unicodeText });
      assert.deepEqual(await unicode.next(), { done: true, value: undefined });
      assert.equal(finishRequests.length, beforeEarlyClose + 1);
      assert.equal(finishRequests.at(-1).step.input.final_message, unicodeText);
      assert.equal([...finishRequests.at(-1).step.input.final_message].length, 8000);
    } finally {
      configured.uninstall();
    }
  } finally {
    await rm(directory, { recursive: true, force: true });
  }
}
