// Modified for Norma.
import { AsyncLocalStorage } from "node:async_hooks";

import { z } from "zod";

import { NormaClient, connectionFor, defaultClient } from "./client";
import type { ActionKind } from "./checkpoint";

export type ActionStatus = "succeeded" | "failed" | "denied" | "unknown";
export interface ActionSummary {
  index: number;
  kind: ActionKind;
  native_tool: string;
  target: string | null;
  command: string | null;
  status: ActionStatus;
  exit_code: number | null;
  output_tail: string | null;
  duration_ms: number | null;
}
export interface ContinuationState { count: number; max: number }
export interface TurnRecord {
  schema_version: "1";
  platform: string;
  platform_version: string | null;
  model: string | null;
  session_id: string;
  turn_id: string;
  user_request: string | null;
  final_message: string;
  actions: ActionSummary[];
  actions_truncated: boolean;
  files_touched: string[];
  evidence_source: "sdk";
  continuation: ContinuationState;
}
export interface FinishVerdict {
  verdict: "pass" | "hold" | "unsure" | "unavailable";
  reason: string;
  reason_codes: string[];
  held_by: number[];
  would_hold_by: number[];
  continuation: ContinuationState;
  allow_finish: boolean;
  outcome: "completed" | "held" | "finished_unverified";
  provider_input_tokens: number | null;
}
export interface RecordAction {
  kind: ActionKind;
  nativeTool: string;
  status: ActionStatus;
  target?: string | null;
  command?: string | null;
  exitCode?: number | null;
  outputTail?: string | null;
  durationMs?: number | null;
}
export interface TurnRecorderOptions {
  sessionId: string;
  turnId: string;
  userRequest?: string;
  platform?: string;
  platformVersion?: string;
  model?: string;
  maxContinuations?: number;
}

const activeRecorder = new AsyncLocalStorage<TurnRecorder>();
const privateKey = /-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----/gi;
const bearer = /Bearer\s+[A-Za-z0-9._~+/-]+={0,2}/gi;
const awsKey = /\b(?:AKIA|ASIA)[A-Z0-9]{16}\b/g;
const gcpKey = /\bAIza[A-Za-z0-9_-]{35}\b/g;
const stripeKey = /\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b/g;
const assignment = /(\b[A-Za-z_][A-Za-z0-9_]*_(?:TOKEN|SECRET|PASSWORD|KEY)\b["']?\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)/gi;

/** Apply the shared turn redaction rules before clipping any evidence. */
export function redactTurnText(value: string): string {
  return value.replace(privateKey, "[REDACTED PRIVATE KEY]")
    .replace(bearer, "Bearer [REDACTED]")
    .replace(awsKey, "[REDACTED AWS KEY]")
    .replace(gcpKey, "[REDACTED GCP KEY]")
    .replace(stripeKey, "[REDACTED STRIPE KEY]")
    .replace(assignment, (_match, prefix: string) => `${prefix}[REDACTED]`);
}

function bounded(value: string | null | undefined, limit: number, tail = false): string | null {
  if (value == null) return null;
  if (value.length > 65_536) return tail ? "[omitted oversized output]" : "[omitted oversized text]";
  const redacted = redactTurnText(value);
  return tail ? redacted.slice(-limit) : redacted.slice(0, limit);
}

export class TurnRecorder {
  private readonly options: Required<Pick<TurnRecorderOptions, "sessionId" | "turnId" | "platform" | "maxContinuations">> & TurnRecorderOptions;
  private readonly actions: ActionSummary[] = [];
  private readonly filesTouched: string[] = [];
  private actionCount = 0;
  private truncated = false;

  constructor(options: TurnRecorderOptions) {
    if (!options.sessionId || !options.turnId) throw new Error("sessionId and turnId are required");
    const max = options.maxContinuations ?? 2;
    if (!Number.isInteger(max) || max < 0 || max > 3) throw new Error("maxContinuations must be between 0 and 3");
    this.options = { ...options, platform: options.platform ?? "typescript-sdk", maxContinuations: max };
  }

  /** Scope automatic decorated-action recording to this async turn. */
  run<T>(work: () => T): T {
    return activeRecorder.run(this, work);
  }

  recordAction(input: RecordAction): ActionSummary {
    const action: ActionSummary = {
      index: this.actionCount++,
      kind: input.kind,
      native_tool: bounded(input.nativeTool, 256) ?? "",
      status: input.status,
      target: bounded(input.target, 300),
      command: bounded(input.command, 500),
      exit_code: input.exitCode ?? null,
      output_tail: bounded(input.outputTail, 1000, true),
      duration_ms: input.durationMs ?? null,
    };
    this.actions.push(action);
    if (this.actions.length > 50) { this.actions.shift(); this.truncated = true; }
    if (action.kind === "edit" && action.status === "succeeded" && action.target && !this.filesTouched.includes(action.target)) {
      this.filesTouched.push(action.target);
      if (this.filesTouched.length > 200) this.filesTouched.shift();
    }
    return { ...action };
  }

  turn(finalMessage: string, continuationCount = 0): TurnRecord {
    if (!Number.isInteger(continuationCount) || continuationCount < 0) throw new Error("continuationCount must be nonnegative");
    return {
      schema_version: "1",
      platform: bounded(this.options.platform, 256) ?? "typescript-sdk",
      platform_version: bounded(this.options.platformVersion, 256),
      model: bounded(this.options.model, 256),
      session_id: bounded(this.options.sessionId, 512) ?? "",
      turn_id: bounded(this.options.turnId, 512) ?? "",
      user_request: bounded(this.options.userRequest, 2000),
      final_message: bounded(finalMessage, 8000) ?? "",
      actions: this.actions.map((action) => ({ ...action })),
      actions_truncated: this.truncated,
      files_touched: [...this.filesTouched],
      evidence_source: "sdk",
      continuation: { count: continuationCount, max: this.options.maxContinuations },
    };
  }
}

/** Record a completed decorated step only when a turn recorder is active. */
export function recordDecoratedAction(input: RecordAction): void {
  activeRecorder.getStore()?.recordAction(input);
}

const continuationSchema = z.strictObject({ count: z.number().int().nonnegative(), max: z.number().int().min(0).max(3) });
const actionSchema = z.strictObject({
  index: z.number().int().nonnegative(),
  kind: z.enum(["shell", "edit", "read", "search", "web", "mcp", "task", "other"]),
  native_tool: z.string().max(256),
  target: z.string().max(300).nullable(),
  command: z.string().max(500).nullable(),
  status: z.enum(["succeeded", "failed", "denied", "unknown"]),
  exit_code: z.number().int().nullable(),
  output_tail: z.string().max(1000).nullable(),
  duration_ms: z.number().int().nonnegative().nullable(),
});
const turnSchema = z.strictObject({
  schema_version: z.literal("1"),
  platform: z.string().min(1).max(256),
  platform_version: z.string().max(256).nullable(),
  model: z.string().max(256).nullable(),
  session_id: z.string().min(1).max(512),
  turn_id: z.string().min(1).max(512),
  user_request: z.string().max(2000).nullable(),
  final_message: z.string().max(8000),
  actions: z.array(actionSchema).max(50),
  actions_truncated: z.boolean(),
  files_touched: z.array(z.string().max(300)).max(200),
  evidence_source: z.literal("sdk"),
  continuation: continuationSchema,
});

const finishVerdictSchema = z.strictObject({
  execution_contract: z.literal("hosted-v1"),
  verdict: z.enum(["pass", "hold", "unsure", "unavailable"]),
  reason: z.string().max(600),
  reason_codes: z.array(z.string().max(100)).max(100),
  held_by: z.array(z.number().int()).max(1000),
  would_hold_by: z.array(z.number().int()).max(1000),
  continuation: continuationSchema,
  allow_finish: z.boolean(),
  outcome: z.enum(["completed", "held", "finished_unverified"]),
  provider_input_tokens: z.number().int().nonnegative().nullable(),
}).superRefine((value, context) => {
  if (value.verdict === "hold" && value.continuation.count >= value.continuation.max) {
    context.addIssue({ code: "custom", message: "hold exceeds continuation limit" });
  }
  const hold = value.verdict === "hold" && value.continuation.count < value.continuation.max;
  const outcome = hold ? "held" : value.verdict === "pass" ? "completed" : "finished_unverified";
  if (value.allow_finish === hold || value.outcome !== outcome) context.addIssue({ code: "custom", message: "inconsistent finish verdict" });
});

function unavailable(continuation: ContinuationState): FinishVerdict {
  const safeContinuation = continuationSchema.safeParse(continuation);
  return {
    verdict: "unavailable",
    reason: "Finish allowed: the Norma service could not complete this check.",
    reason_codes: ["unavailable"], held_by: [], would_hold_by: [],
    continuation: safeContinuation.success ? safeContinuation.data : { count: 0, max: 0 },
    allow_finish: true, outcome: "finished_unverified", provider_input_tokens: null,
  };
}

async function boundedResponse(response: Response): Promise<unknown> {
  const length = Number(response.headers.get("content-length"));
  if (length > 256 * 1024) throw new Error("finish response too large");
  if (!response.body) throw new Error("finish response has no body");
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    let reading = true;
    while (reading) {
      const { done, value } = await reader.read();
      if (done) { reading = false; continue; }
      size += value.byteLength;
      if (size > 256 * 1024) throw new Error("finish response too large");
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  return JSON.parse(new TextDecoder().decode(bytes));
}

function redactTurn(turn: TurnRecord): TurnRecord {
  return {
    ...turn,
    platform: bounded(turn.platform, 256) ?? "typescript-sdk",
    platform_version: bounded(turn.platform_version, 256),
    model: bounded(turn.model, 256),
    session_id: bounded(turn.session_id, 512) ?? "",
    turn_id: bounded(turn.turn_id, 512) ?? "",
    user_request: bounded(turn.user_request, 2000),
    final_message: bounded(turn.final_message, 8000) ?? "",
    actions: turn.actions.slice(-50).map((action) => ({
      ...action,
      native_tool: bounded(action.native_tool, 256) ?? "",
      target: bounded(action.target, 300),
      command: bounded(action.command, 500),
      output_tail: bounded(action.output_tail, 1000, true),
    })),
    actions_truncated: turn.actions_truncated || turn.actions.length > 50,
    files_touched: turn.files_touched.slice(-200).map((path) => bounded(path, 300) ?? ""),
  };
}

/** Finish checks fail open on network, auth, timeout, and invalid response failures. */
export async function checkFinish(
  turn: TurnRecord,
  options: { client?: NormaClient; agentName?: string; targetType?: string; targetId?: string } = {},
): Promise<FinishVerdict> {
  const continuation = turn?.continuation ?? { count: 0, max: 0 };
  const client = options.client ?? defaultClient;
  const config = connectionFor(client);
  if (!config || !(options.agentName ?? config.agentName)) return unavailable(continuation);
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error("finish deadline exceeded")); }, 8_000);
  });
  try {
    const sanitized = redactTurn(turn);
    const checkedTurn = turnSchema.safeParse(sanitized);
    if (!checkedTurn.success) return unavailable(continuation);
    const targetType = options.targetType ?? config.targetType;
    const targetId = options.targetId ?? config.targetId;
    if (Boolean(targetType) !== Boolean(targetId)) return unavailable(continuation);
    const request = {
      execution_mode: "hosted",
      agent_name: options.agentName ?? config.agentName,
      stage: "pre",
      step: { type: "turn", name: `${sanitized.platform}.finish`, input: checkedTurn.data, output: null },
      ...(targetType && targetId ? { target_type: targetType, target_id: targetId } : {}),
    };
    const payload = JSON.stringify(request);
    if (new TextEncoder().encode(payload).length > 256 * 1024) return unavailable(continuation);
    const work = async (): Promise<FinishVerdict> => {
      const key = typeof config.agentKey === "function" ? await config.agentKey() : config.agentKey;
      if (controller.signal.aborted) return unavailable(continuation);
      if (!key) return unavailable(continuation);
      const response = await fetch(new Request(new URL("api/v1/checks", `${config.runtimeUrl.replace(/\/$/, "")}/`), {
        method: "POST", redirect: "error",
        headers: { "Content-Type": "application/json", "X-API-Key": key },
        body: payload,
        signal: controller.signal,
      }));
      if (!response.ok) return unavailable(continuation);
      const parsed = finishVerdictSchema.safeParse(await boundedResponse(response));
      if (!parsed.success || parsed.data.continuation.count !== continuation.count || parsed.data.continuation.max > continuation.max) return unavailable(continuation);
      return { ...parsed.data, reason: bounded(parsed.data.reason, 600) ?? "" };
    };
    return await Promise.race([work(), deadline]);
  } catch {
    controller.abort();
    return unavailable(continuation);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
