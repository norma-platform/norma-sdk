// Modified for Norma.
import { afterEach, describe, expect, it, vi } from "vitest";

import { NormaClient, checkpoint } from "../src/index";

function initializedClient(): NormaClient {
  const client = new NormaClient();
  client.init({ agentName: "quote-agent",  runtimeUrl: "http://localhost:8000", agentKey: "test" });
  return client;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function safeResponse(): Response {
  return new Response(JSON.stringify({ confidence: 1, execution_contract: "hosted-v1", allowed: true, matches: [], errors: [] }), {
    status: 200, headers: { "content-type": "application/json" },
  });
}

describe("checkpoint argument snapshots", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it.each(["tool", "llm"] as const)("executes approved %s values despite mutations during pre and post checks", async (stepType) => {
    // Given: actual generated HTTP serialization, with both checks held pending.
    const preReceived = deferred<void>();
    const postReceived = deferred<void>();
    const preResponse = deferred<Response>();
    const postResponse = deferred<Response>();
    const inputs: unknown[] = [];
    const fetchMock = vi.fn(async (request: Request) => {
      const body = await request.json();
      inputs.push(body.step.input);
      if (body.stage === "pre") {
        preReceived.resolve();
        return preResponse.promise;
      }
      postReceived.resolve();
      return postResponse.promise;
    });
    vi.stubGlobal("fetch", fetchMock);
    const quote = { lines: [{ price: 450, labels: ["bounded"] }] };
    let executedPrice: number | undefined;
    let executionCopy: typeof quote | undefined;
    const execute = vi.fn(async (value: typeof quote) => {
      executedPrice = value.lines[0].price;
      executionCopy = value;
      value.lines[0].price = 475;
      return "sent";
    });
    const wrapped = checkpoint(execute, { client: initializedClient(), stepName: "sendQuote", stepType });

    // When: the caller edits nested data while precheck waits, then again during postcheck.
    const result = wrapped(quote);
    await preReceived.promise;
    quote.lines[0].price = 900;
    quote.lines[0].labels.push("unlimited");
    preResponse.resolve(safeResponse());
    await postReceived.promise;
    quote.lines[0].price = 1200;
    executionCopy!.lines[0].price = 600;
    postResponse.resolve(safeResponse());

    // Then: execution starts with approved values and both wire inputs retain the original.
    await expect(result).resolves.toBe("sent");
    const approved = [{ lines: [{ price: 450, labels: ["bounded"] }] }];
    expect(inputs).toEqual([stepType === "llm" ? approved : { args: approved }, stepType === "llm" ? approved : { args: approved }]);
    expect(executedPrice).toBe(450);
    expect(executionCopy).not.toBe(quote);
    expect(quote.lines[0].price).toBe(1200);
    expect(execute).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("derives action facts from the immutable approved values", async () => {
    // Given: action metadata keeps a reference while the precheck is pending.
    const preReceived = deferred<void>();
    const preResponse = deferred<Response>();
    let actionArgument: { destination: { path: string } } | undefined;
    const fetchMock = vi.fn(async () => {
      if (fetchMock.mock.calls.length === 1) { preReceived.resolve(); return preResponse.promise; }
      return safeResponse();
    });
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async (value: { destination: { path: string } }) => value.destination.path);
    const wrapped = checkpoint(execute, {
      client: initializedClient(),
      action: (value) => { actionArgument = value; return { kind: "edit", target: value.destination.path }; },
    });

    // When: a holder of the callback argument attempts to alter the approved destination.
    const result = wrapped({ destination: { path: "allowed.txt" } });
    await preReceived.promise;
    expect(() => { actionArgument!.destination.path = "different.txt"; }).toThrow(TypeError);
    preResponse.resolve(safeResponse());

    // Then: it cannot change either the approved input or executed destination.
    await expect(result).resolves.toBe("allowed.txt");
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it("preserves the receiver and JSON aliases without exposing caller objects", async () => {
    // Given: a method uses its receiver and two references to one nested JSON object.
    vi.stubGlobal("fetch", vi.fn(async () => safeResponse()));
    const shared = { count: 1 };
    const owner = {
      prefix: "owner",
      call: checkpoint(async function (this: { prefix: string }, left: typeof shared, right: typeof shared) {
        expect(left).toBe(right);
        left.count++;
        return `${this.prefix}:${right.count}`;
      }, { client: initializedClient() }),
    };

    // When: invoking the guarded method normally.
    const result = await owner.call(shared, shared);

    // Then: the receiver and alias relationship survive; the caller's value stays untouched.
    expect(result).toBe("owner:2");
    expect(shared.count).toBe(1);
  });

  it("fails closed without initialization", async () => {
    const effect = vi.fn(async () => "effect");
    await expect(checkpoint(effect)()).rejects.toThrow(/not initialized/);
    expect(effect).not.toHaveBeenCalled();
  });

  it("preserves plain JSON values and own prototype-named keys", async () => {
    // Given: valid JSON can contain a __proto__ key or use a null-prototype dictionary.
    vi.stubGlobal("fetch", vi.fn(async () => safeResponse()));
    const dictionary = Object.create(null) as { [key: string]: unknown };
    dictionary.values = [null, true, false, 0, 1.25, ""];
    dictionary.__proto__ = { destination: "approved" };
    const execute = vi.fn(async (value: typeof dictionary) => {
      expect(Object.getPrototypeOf(value)).toBeNull();
      expect(Object.hasOwn(value, "__proto__")).toBe(true);
      expect(value).toEqual(dictionary);
      expect(value).not.toBe(dictionary);
      return "ran";
    });
    // When: evaluating and executing the valid data.
    const result = await checkpoint(execute, { client: initializedClient() })(dictionary);
    // Then: values and own keys survive without prototype mutation.
    expect(result).toBe("ran");
    expect(execute).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["undefined", undefined], ["NaN", NaN], ["infinity", Infinity], ["negative zero", -0],
    ["bigint", 1n], ["function", () => 1], ["symbol", Symbol("value")],
    ["Date", new Date(0)], ["Map", new Map()], ["Set", new Set()],
    ["sparse array", Array(1)], ["decorated array", Object.assign([1], { extra: 2 })],
    ["hidden field", Object.defineProperty({}, "hidden", { value: 1 })],
    ["symbol field", { [Symbol("hidden")]: 1 }],
  ])("rejects %s before check or execution", async (_name, value) => {
    // Given: a value whose JSON conversion would omit or change its meaning.
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async (argument: unknown) => argument);
    // When: invoking the initialized wrapper.
    await expect(checkpoint(execute, { client: initializedClient() })({ nested: value })).rejects.toThrow(/Checkpoint arguments/);
    // Then: no approval request or side effect occurs.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects accessors without invoking them", async () => {
    // Given: a getter could change values or cause side effects when read.
    const getter = vi.fn(() => "unstable");
    const value = Object.defineProperty({}, "path", { enumerable: true, get: getter });
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async (argument: unknown) => argument);
    // When: snapshotting through the public wrapper.
    await expect(checkpoint(execute, { client: initializedClient() })(value)).rejects.toThrow(/Checkpoint arguments/);
    // Then: neither the accessor, transport, nor tool ran.
    expect(getter).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });

  it("rejects cycles before check or execution", async () => {
    // Given: JSON cannot represent a cycle.
    const cycle: unknown[] = [];
    cycle.push(cycle);
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async (argument: unknown) => argument);
    // When: invoking the public wrapper.
    await expect(checkpoint(execute, { client: initializedClient() })(cycle)).rejects.toThrow(/Checkpoint arguments/);
    // Then: it fails closed before side effects.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(execute).not.toHaveBeenCalled();
  });

  it("fails closed on a transport error without retrying the check or running the tool", async () => {
    // Given: the real generated transport cannot reach the runtime.
    const fetchMock = vi.fn(async () => { throw new TypeError("network unavailable"); });
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async () => "ran");
    // When: invoking the initialized wrapper.
    await expect(checkpoint(execute, { client: initializedClient() })()).rejects.toThrow(/Norma check failed during pre/);
    // Then: the default no-retry policy makes exactly one request and no tool call.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(execute).not.toHaveBeenCalled();
  });

  it("fails closed when the runtime omits its approval decision", async () => {
    // Given: a 200 response without the required allowed decision.
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ confidence: 1, matches: [] }), {
      status: 200, headers: { "content-type": "application/json" },
    }));
    vi.stubGlobal("fetch", fetchMock);
    const execute = vi.fn(async () => "ran");
    // When: the real generated codec rejects a missing approval boolean.
    await expect(checkpoint(execute, { client: initializedClient() })()).rejects.toThrow(/Norma check failed during pre/);
    // Then: invalid approval never authorizes the tool or an automatic retry.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(execute).not.toHaveBeenCalled();
  });
});
