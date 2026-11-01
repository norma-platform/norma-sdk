// Modified for Norma.
import { CheckError } from "./errors";

export interface StreamTextSelection {
  present: boolean;
  value?: unknown;
}

export interface ConfiguredStreamSession {
  run<T>(work: () => T): T;
  assertActive(): void;
  start(): Promise<void>;
  readonly iterator?: AsyncIterator<unknown>;
  selectText(chunk: unknown): StreamTextSelection | Promise<StreamTextSelection>;
  finalize(finalMessage: string): Promise<void>;
}

export interface ConfiguredStreamOptions {
  mode: "concat" | "last";
  maxChars: number;
  missing: "error" | "skip";
  open(): Promise<ConfiguredStreamSession>;
}

function countCodePointsUpTo(value: string, limit: number): number {
  let count = 0;
  for (let index = 0; index < value.length;) {
    const codePoint = value.codePointAt(index)!;
    index += codePoint > 0xffff ? 2 : 1;
    count += 1;
    if (count > limit) return count;
  }
  return count;
}

/**
 * A single-consumer async iterator that scopes each advance and terminal check,
 * forwards every application result unchanged, and stores only bounded text.
 */
export function configuredAsyncStream(options: ConfiguredStreamOptions): AsyncIterableIterator<unknown> {
  let state: "active" | "aborted" | "terminal" = "active";
  let abortRequested = false;
  let sessionPromise: Promise<ConfiguredStreamSession> | undefined;
  let started = false;
  let cleanupAttempted = false;
  let queued: Promise<unknown> = Promise.resolve();
  let finalMessage = "";
  const isAborted = (): boolean => state === "aborted";

  const session = (): Promise<ConfiguredStreamSession> => {
    if (!sessionPromise) sessionPromise = options.open();
    return sessionPromise;
  };

  const enqueue = <T>(operation: () => Promise<T>): Promise<T> => {
    const result = queued.then(operation, operation);
    queued = result.then(() => undefined, () => undefined);
    return result;
  };

  const iteratorFor = (current: ConfiguredStreamSession): AsyncIterator<unknown> => {
    const iterator = current.iterator;
    if (!iterator || typeof iterator.next !== "function") {
      throw new TypeError("Streaming entrypoint did not produce an async iterator.");
    }
    return iterator;
  };

  const closeAfterFailure = async (current: ConfiguredStreamSession): Promise<void> => {
    if (cleanupAttempted) return;
    cleanupAttempted = true;
    const iterator = current.iterator;
    const returnMethod = iterator?.return;
    if (typeof returnMethod !== "function") return;
    try {
      await current.run(() => Reflect.apply(returnMethod, iterator, []));
    } catch {
      // Preserve the failure that caused cleanup.
    }
  };

  function requireObjectResult(result: unknown): asserts result is IteratorResult<unknown> {
    if ((typeof result !== "object" && typeof result !== "function") || result === null) {
      throw new TypeError("Async iterator returned a non-object result.");
    }
  }

  const accumulate = async (current: ConfiguredStreamSession, chunk: unknown): Promise<void> => {
    const selection = await current.selectText(chunk);
    current.assertActive();
    if (isAborted() || abortRequested) return;
    if (!selection.present || selection.value === null) {
      if (options.missing === "skip") return;
      throw new CheckError({ stage: "post", message: "Streaming finish text selector did not resolve to a value." });
    }
    if (typeof selection.value !== "string") {
      throw new CheckError({ stage: "post", message: "Streaming finish text selector must resolve to a string or null." });
    }
    const chunkChars = countCodePointsUpTo(selection.value, options.maxChars);
    if (chunkChars > options.maxChars) {
      throw new CheckError({ stage: "post", message: `Streaming finish text exceeds max_chars (${options.maxChars} Unicode code points).` });
    }
    if (options.mode === "last") {
      finalMessage = selection.value;
      return;
    }
    const combined = finalMessage + selection.value;
    const combinedChars = countCodePointsUpTo(combined, options.maxChars);
    if (combinedChars > options.maxChars) {
      throw new CheckError({ stage: "post", message: `Streaming finish text exceeds max_chars (${options.maxChars} Unicode code points).` });
    }
    finalMessage = combined;
  };

  const closeOnTextFailure = async (current: ConfiguredStreamSession, error: unknown): Promise<never> => {
    state = "terminal";
    await closeAfterFailure(current);
    throw error;
  };

  const getActiveSession = async (): Promise<ConfiguredStreamSession> => {
    const current = await session();
    if (state === "active" && !abortRequested) current.assertActive();
    if (!started && state === "active" && !abortRequested) {
      started = true;
      await current.run(async () => {
        current.assertActive();
        await current.start();
        current.assertActive();
      });
    }
    if (state === "active" && !abortRequested) current.assertActive();
    if ((isAborted() || abortRequested) && !current.iterator) return current;
    iteratorFor(current);
    return current;
  };

  const advance = async (sent?: unknown, kind: "next" | "throw" = "next"): Promise<IteratorResult<unknown>> => {
    if (state === "terminal") return { done: true, value: undefined };
    if ((isAborted() || abortRequested) && !sessionPromise) {
      state = "terminal";
      return { done: true, value: undefined };
    }
    let current: ConfiguredStreamSession;
    try {
      current = await getActiveSession();
    } catch (error) {
      state = "terminal";
      if (sessionPromise) {
        try { await closeAfterFailure(await sessionPromise); } catch { /* Preserve the opening failure. */ }
      }
      throw error;
    }
    if ((isAborted() || abortRequested) && state === "active") return { done: true, value: undefined };
    if (!current.iterator) return { done: true, value: undefined };
    const iterator = iteratorFor(current);
    try {
      const cleanupAdvance = isAborted();
      if (kind === "throw" && typeof iterator.throw !== "function") {
        state = "aborted";
        await closeAfterFailure(current);
        throw sent;
      }
      const result = await current.run(async () => {
        if (!cleanupAdvance) current.assertActive();
        if (kind === "throw") {
          const throwMethod = iterator.throw!;
          const thrown = await Reflect.apply(throwMethod, iterator, [sent]);
          if (!cleanupAdvance) current.assertActive();
          return thrown;
        }
        const nextMethod = iterator.next;
        const next = await Reflect.apply(nextMethod, iterator, [sent]);
        if (!cleanupAdvance) current.assertActive();
        return next;
      });
      requireObjectResult(result);
      const done = Boolean(result.done);
      const cancellationRequested = isAborted() || abortRequested;
      if (cancellationRequested && state === "active") state = "aborted";
      if (done) {
        if (cancellationRequested) {
          state = "terminal";
          return result;
        }
        state = "terminal";
        await current.run(async () => {
          current.assertActive();
          await current.finalize(finalMessage);
          current.assertActive();
        });
        return result;
      }
      if (state === "active" && !abortRequested) {
        try {
          await current.run(() => accumulate(current, result.value));
          current.assertActive();
        } catch (error) {
          return await closeOnTextFailure(current, error);
        }
      }
      return result;
    } catch (error) {
      if (state !== "terminal") {
        state = "terminal";
        await closeAfterFailure(current);
      }
      throw error;
    }
  };

  const stream: AsyncIterableIterator<unknown> = {
    [Symbol.asyncIterator]() { return this; },
    next(value?: unknown): Promise<IteratorResult<unknown>> {
      return enqueue(() => advance(value));
    },
    throw(error?: unknown): Promise<IteratorResult<unknown>> {
      return enqueue(() => {
        if (state === "terminal") return Promise.reject(error);
        return advance(error, "throw");
      });
    },
    return(value?: unknown): Promise<IteratorResult<unknown>> {
      // Record cancellation before queueing cleanup so an in-flight next cannot
      // mistake its eventual done result for natural stream exhaustion.
      if (state !== "terminal") abortRequested = true;
      return enqueue(async () => {
        if (state === "terminal") return { done: true, value };
        if (!sessionPromise) {
          state = "terminal";
          return { done: true, value };
        }
        const current = await session();
        state = "aborted";
        const iterator = current.iterator;
        const returnMethod = iterator?.return;
        if (typeof returnMethod !== "function") {
          state = "terminal";
          return { done: true, value };
        }
        cleanupAttempted = true;
        try {
          const result = await current.run(() => Reflect.apply(returnMethod, iterator, [value]));
          requireObjectResult(result);
          if (result.done) state = "terminal";
          return result;
        } catch (error) {
          state = "terminal";
          throw error;
        }
      });
    },
  };
  return stream;
}
