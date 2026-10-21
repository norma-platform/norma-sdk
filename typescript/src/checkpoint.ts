// Modified for Norma.
import { NormaClient, defaultClient } from "./client";
import { copyCheckpointArguments } from "./checkpoint-arguments";
import { copyCheckpointState } from "./checkpoint-state";
import { CheckError, RuleViolationError, RuleSteerError } from "./errors";
import { recordDecoratedAction, redactTurnText } from "./finish";
import { jsonValueSchema } from "./json-contract";
import type { JsonObject, JsonValue } from "./types";
import type { Decision } from "./public-api";

export type ActionKind = "shell" | "edit" | "read" | "search" | "web" | "mcp" | "task" | "other";

export interface NormaAction {
  kind: ActionKind;
  target?: string | null;
  command?: string | null;
  cwd?: string | null;
}

export interface CheckpointOptions<TArgs extends unknown[] = unknown[]> {
  client?: NormaClient;
  stepName?: string;
  stepType?: "tool" | "llm";
  targetType?: string;
  targetId?: string;
  /** Caller-owned facts about this action; the SDK never guesses that a function runs shell/edit. */
  action?: NormaAction | ((...args: TArgs) => NormaAction);
  /**
   * Explicit observed state, or a callback resolved once before this invocation's precheck.
   * Only plain JSON is accepted (64 KiB, 32 levels, 10000 nodes, safe integers).
   * Postchecks reuse the snapshot.
   * State is evaluated regardless of retention settings; it does not reserve or update data.
   */
  state?: JsonObject | ((...args: TArgs) => JsonObject | Promise<JsonObject>);
}

export type AsyncFn<TArgs extends unknown[], TResult> = (...args: TArgs) => Promise<TResult>;

function normalizedAction<TArgs extends unknown[]>(
  action: CheckpointOptions<TArgs>["action"], args: TArgs,
): { kind: ActionKind; target: string | null; command: string | null; cwd: string | null } {
  const provided = typeof action === "function" ? action(...args) : action;
  const text = (value: string | null | undefined, limit: number): string | null =>
    value == null ? null : value.length > 65_536 ? "[omitted oversized text]" : redactTurnText(value).slice(0, limit);
  return {
    kind: provided?.kind ?? "other",
    target: text(provided?.target, 300),
    command: text(provided?.command, 500),
    cwd: text(provided?.cwd, 300),
  };
}

export function enforceCheckpointDecision(result: Decision, stage: "pre" | "post"): void {
  const context = { stage, decision: result, recoveryFeedback: result.recoveryFeedback };
  const blocked = stage === "pre" ? "Execution blocked for safety." : "Action already ran; result blocked for safety.";
  const errors = result.checks.filter(item => item.mode !== "observe" && (item.status === "error" || item.result.error != null));
  if (errors.length) {
    const checkIds = errors.flatMap(item => item.checkId === undefined ? [] : [item.checkId]);
    throw new CheckError({ ...context, checkIds: checkIds.length ? checkIds : undefined,
      message: `Norma check failed during ${stage}. ${blocked} Review the check details in Norma before retrying.` });
  }
  const triggered = result.checks.filter(item => item.mode !== "observe" && (item.status === "matched" || item.status === "unresolved")).map(item => ({
    action: item.effectiveAction ?? item.action, id: item.ruleId, name: item.ruleName, executionId: item.checkId,
    reason: item.steeringContext?.message ?? item.result.message ?? result.reason ?? "Rule matched",
  }));
  const blocking = triggered.find((item) => item.action === "deny")
    ?? triggered.find((item) => item.action === "steer");
  if (blocking) {
    throw new (blocking.action === "steer" ? RuleSteerError : RuleViolationError)({
      ...context,
      ruleName: blocking.name,
      ruleId: String(blocking.id),
      checkId: blocking.executionId,
      action: blocking.action === "deny" ? "deny" : "steer",
      decision: {...result, allowed: false, reason: blocking.reason},
      message: `${blocking.name}: ${blocking.reason}`,
    });
  }
  if (!result.allowed) {
    throw new CheckError({
      ...context,
      message: `Norma check returned an unsafe result without an actionable decision during ${stage}. ${blocked}`,
    });
  }
}

/** Evaluate immutable JSON arguments, then execute a private copy with the same receiver. */
function wrapCheckpoint<TArgs extends unknown[], TResult>(
  fn: AsyncFn<TArgs, TResult>,
  options: CheckpointOptions<TArgs> = {},
): AsyncFn<TArgs, TResult> {
  if ("policy" in options) throw new TypeError("Attach a Policy or Rule to the agent in Norma; checkpoint options do not select a policy.");
  return async function (this: unknown, ...args: TArgs): Promise<TResult> {
    const client = options.client ?? defaultClient;
    if (!client.initialized) throw new Error("Norma client is not initialized. Call init() before using a checkpoint.");
    const name = options.stepName ?? fn.name ?? "anonymous";
    const approvedArgs = client.initialized ? copyCheckpointArguments(args, true) : args;
    const action = normalizedAction(options.action, approvedArgs);
    const started = performance.now();
    let ran = false;
    let completed = false;
    let output: TResult | undefined;
    let failed: unknown;
    try {
    const agentName = client.config?.agentName;
    if (!agentName) throw new Error("Norma client has no agent name.");
    const targetType = options.targetType ?? client.config?.targetType;
    const targetId = options.targetId ?? client.config?.targetId;
    if (Boolean(targetType) !== Boolean(targetId)) throw new Error("targetType and targetId must be supplied together.");
    let state: JsonObject | undefined;
    if (options.state !== undefined) {
      try {
        const provided = typeof options.state === "function" ? options.state(...approvedArgs) : options.state;
        // Copy synchronous values before yielding so caller mutation cannot change the snapshot.
        state = copyCheckpointState(provided instanceof Promise ? await provided : provided);
      } catch {
        // Callback exceptions may contain application secrets; do not echo their messages.
        throw new CheckError({
          stage: "pre",
          message: "Norma state capture failed; execution blocked. Supply a plain finite JSON object with safe integers within 32 levels, 10000 nodes and 65536 UTF-8 bytes, or check the state callback.",
        });
      }
    }
    const step = {
      type: options.stepType ?? "tool",
      name,
      // copyCheckpointArguments already validated the complete JSON tree.
      input: (options.stepType === "llm" ? approvedArgs : Object.freeze({ args: approvedArgs })) as JsonValue,
      context: options.stepType === "llm" ? null : {
        norma_action: action,
        // Disambiguate this SDK envelope from named arguments that happen to contain "args".
        norma_argument_shape: "positional",
      },
    };
    const evaluate = async (stage: "pre" | "post", output?: TResult): Promise<void> => {
      let result: Decision;
      try {
        result = await client.checks.check({
          targetType,
          targetId,
          stage,
          step: {
            ...step,
            context: state === undefined ? step.context : { ...step.context, state: copyCheckpointState(state) },
            output: stage === "post" ? jsonValueSchema.parse(output ?? null) : null,
          },
        });
      } catch (cause) {
        throw new CheckError({
          stage, cause,
          message: `Norma check failed during ${stage}; ${stage === "pre" ? "execution blocked" : "action already ran; result blocked"}.`,
        });
      }
      enforceCheckpointDecision(result, stage);
    };
    await evaluate("pre");
    const executionArgs = copyCheckpointArguments(approvedArgs, false);
    ran = true;
    output = await fn.apply(this, executionArgs);
    completed = true;
    await evaluate("post", output);
    return output;
    } catch (error) {
      failed = error;
      throw error;
    } finally {
      const exitCodeValue = completed && typeof output === "object" && output !== null
        ? "exit_code" in output ? output.exit_code : "returncode" in output ? output.returncode : null
        : null;
      const exitCode = typeof exitCodeValue === "number" && Number.isInteger(exitCodeValue) ? exitCodeValue : null;
      const isError = completed && typeof output === "object" && output !== null && "isError" in output && output.isError === true;
      const status = completed
        ? isError || (exitCode !== null && exitCode !== 0) ? "failed" : "succeeded"
        : ran ? "failed" : failed instanceof RuleViolationError ? "denied" : "unknown";
      recordDecoratedAction({
        kind: action.kind, nativeTool: name, status,
        target: action.target, command: action.command, exitCode,
        outputTail: typeof output === "string" ? output : failed instanceof Error ? failed.name : null,
        durationMs: Math.max(0, Math.round(performance.now() - started)),
      });
    }
  };
}


/** Check an async action before and after execution; requires a configured Norma client. */
export function checkpoint<TArgs extends unknown[], TResult>(
  fn: AsyncFn<TArgs, TResult>, options: CheckpointOptions<TArgs> = {},
): AsyncFn<TArgs, TResult> {
  return wrapCheckpoint(fn, { ...options, client: options.client ?? defaultClient });
}
