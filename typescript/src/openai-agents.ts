// Modified for Norma.
/** Optional integration for @openai/agents 0.20.0 custom JSON function tools. */
import {
  tool, defineToolInputGuardrail, defineToolOutputGuardrail,
  ToolGuardrailFunctionOutputFactory as behavior,
  type FunctionTool, type ToolInputParameters, type ToolInputGuardrailData, type ToolInputGuardrailDefinition,
  type ToolOutputGuardrailDefinition, type RunContext,
} from "@openai/agents";
import type { NormaClient } from "./client";
import type { NormaAction } from "./checkpoint";
import type { Decision } from "./public-api";
import type { JsonObject, JsonValue } from "./types";
import { jsonValueSchema } from "./json-contract";
import { enforceCheckpointDecision } from "./checkpoint";
import { copyCheckpointArguments } from "./checkpoint-arguments";
import { RuleSteerError, RuleViolationError } from "./errors";
import { redactTurnText } from "./finish";
import { recordDecoratedAction } from "./finish";

export type NormaToolParameters = Extract<ToolInputParameters, { type: "object"; additionalProperties: false }>;
export interface NormaToolRecord {
  agentName: string;
  frameworkAgentName: string;
  toolName: string;
  callId: string;
  stage: "pre" | "post";
  status: "allowed" | "denied" | "steered" | "error";
  /** Post records describe checks after effects, never rollback or independent verification. */
  decision?: Decision;
}
export interface NormaToolRequestContext {
  targetType?: string;
  targetId?: string;
  traceId?: string;
  spanId?: string;
  context?: JsonObject;
}
export interface NormaToolOptions<Context> {
  name: string;
  description: string;
  /** Plain JSON schema only. Transforming Zod/Standard Schema inputs are intentionally unsupported. */
  parameters: NormaToolParameters;
  /** Application-owned mapping; never derive credentials or tenants from model arguments. */
  resolveClient(data: ToolInputGuardrailData<Context>): NormaClient | undefined;
  /** Optional per-call target/trace snapshot reused by the postcheck. */
  resolveRequestContext?(data: ToolInputGuardrailData<Context>): NormaToolRequestContext;
  /** Snapshot one caller-declared action from validated proposal arguments before the precheck. */
  resolveAction?(invocation: { args: unknown[]; kwargs: Record<string, never>; context: JsonObject; agent_name: string }): Promise<NormaAction> | NormaAction;
  /** Internal lifecycle fence for tools installed by configure(). */
  isActive?(): boolean;
  execute(args: JsonObject, context: RunContext<Context>, signal?: AbortSignal): Promise<JsonValue>;
  needsApproval?: boolean;
  inputGuardrails?: ToolInputGuardrailDefinition<Context>[];
  outputGuardrails?: ToolOutputGuardrailDefinition<Context>[];
  /** Awaited before proceeding. Store only where the application permits decision evidence. */
  onRecord?(record: NormaToolRecord): void | Promise<void>;
}
interface Permission {
  client: NormaClient; session: number; input: JsonObject; fingerprint: string; agentName: string; frameworkAgentName: string;
  requestContext: NormaToolRequestContext; executed: boolean; action?: NormaAction;
}
function objectSnapshot(value: unknown, freeze = false): JsonObject {
  const parsed = jsonValueSchema.parse(value);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new TypeError("Tool arguments must be a JSON object");
  return copyCheckpointArguments([parsed], freeze)[0];
}
/** Create an explicitly protected function tool; does not instrument hosted tools or handoffs. */
export function normaTool<Context = unknown>(options: NormaToolOptions<Context>): FunctionTool<Context, NormaToolParameters, JsonValue> {
  if (!options.parameters || options.parameters.type !== "object" || options.parameters.additionalProperties !== false
      || "parse" in options.parameters || "~standard" in options.parameters) {
    throw new TypeError("normaTool requires a plain strict JSON object schema");
  }
  const checking = new WeakMap<RunContext<Context>, Set<string>>();
  const consumed = new WeakMap<RunContext<Context>, Set<string>>();
  const calls = new WeakMap<RunContext<Context>, Map<string, Permission>>();
  const input = defineToolInputGuardrail<Context>({ name: `norma:${options.name}:pre`, async run(data) {
    const callId = data.toolCall.callId;
    const pending = calls.get(data.context) ?? new Map<string, Permission>();
    calls.set(data.context, pending);
    const busy = checking.get(data.context) ?? new Set<string>();
    checking.set(data.context, busy);
    if (busy.has(callId) || consumed.get(data.context)?.has(callId)) {
      const invalid = consumed.get(data.context) ?? new Set<string>();
      consumed.set(data.context, invalid); invalid.add(callId);
      pending.delete(callId);
      return behavior.throwException({ framework: "norma", callId, stage: "pre" });
    }
    busy.add(callId);
    pending.delete(callId);
    let decision: Decision | undefined;
    let agentName = "unresolved";
    let action: NormaAction | undefined;
    try {
      const client = options.resolveClient(data);
      if (!client?.initialized || !client.config?.agentName) throw new Error("Norma client must be initialized for the active agent");
      const session = client.connectionRevision;
      agentName = client.config.agentName;
      const resolvedContext = options.resolveRequestContext?.(data) ?? {};
      const requestContext: NormaToolRequestContext = {
        ...resolvedContext,
        ...(resolvedContext.context === undefined ? {} : { context: objectSnapshot(resolvedContext.context, true) }),
      };
      if (Boolean(requestContext.targetType) !== Boolean(requestContext.targetId)
          || [requestContext.targetType, requestContext.targetId].some(value => value !== undefined && (typeof value !== "string" || !value.trim()))) {
        throw new TypeError("OpenAI tool target scope must contain a nonblank type and ID together.");
      }
      if (Boolean(requestContext.traceId) !== Boolean(requestContext.spanId)
          || (requestContext.traceId !== undefined && (!/^(?!0{32})[0-9a-fA-F]{32}$/.test(requestContext.traceId)
            || !requestContext.spanId || !/^(?!0{16})[0-9a-fA-F]{16}$/.test(requestContext.spanId)))) {
        throw new TypeError("OpenAI tool trace IDs must be paired, nonzero W3C hexadecimal identifiers.");
      }
      if (data.toolCall.name !== options.name || !callId) throw new Error("Tool call identity mismatch");
      const args = objectSnapshot(JSON.parse(data.toolCall.arguments), true);
      action = options.resolveAction
        ? await options.resolveAction({ args: [args], kwargs: {}, context: requestContext.context ?? {}, agent_name: agentName })
        : undefined;
      if (action?.cwd !== undefined) throw new TypeError("OpenAI native tool actions do not retain cwd in TurnRecord evidence.");
      if (!client.initialized || client.connectionRevision !== session) throw new Error("Norma session changed while resolving the configured tool action");
      decision = await client.checks.check({ stage: "pre", step: {
        type: "tool", name: options.name, input: { args: [args] },
        context: { ...(requestContext.context ?? {}), framework: "openai-agents", agent_name: agentName, tool_call_id: callId },
      }, targetType: requestContext.targetType, targetId: requestContext.targetId, traceId: requestContext.traceId, spanId: requestContext.spanId });
      if (!client.initialized || client.connectionRevision !== session) throw new Error("Norma session changed during check");
      if (options.isActive && !options.isActive()) throw new Error("Configured Norma adapter is closed; execution was blocked.");
      enforceCheckpointDecision(decision, "pre");
      await options.onRecord?.({ agentName, frameworkAgentName: data.agent.name, toolName: options.name, callId, stage: "pre", status: "allowed", decision });
      if (!client.initialized || client.connectionRevision !== session) throw new Error("Norma session changed during recording");
      if (consumed.get(data.context)?.has(callId)) throw new Error("Duplicate tool call identity");
      pending.set(callId, { client, session, input: args, fingerprint: JSON.stringify(args), agentName, frameworkAgentName: data.agent.name, requestContext, executed: false, ...(action ? { action } : {}) });
      return behavior.allow();
    } catch (error) {
      const steered = error instanceof RuleSteerError;
      if (action) {
        const blocked = error instanceof RuleViolationError || steered;
        recordDecoratedAction({ kind: action.kind, nativeTool: options.name, status: blocked ? "denied" : "unknown",
          target: action.target, command: action.command, outputTail: error instanceof Error ? error.name : "error" });
      }
      await options.onRecord?.({ agentName, frameworkAgentName: data.agent.name, toolName: options.name, callId, stage: "pre",
        status: steered ? "steered" : error instanceof RuleViolationError ? "denied" : "error", decision });
      return steered
        ? behavior.rejectContent(redactTurnText(error.decision.reason ?? "Revise the tool arguments to satisfy Norma policy.").slice(0, 500))
        : behavior.throwException({ framework: "norma", callId, stage: "pre" });
    } finally { busy.delete(callId); }
  }});
  const output = defineToolOutputGuardrail<Context>({ name: `norma:${options.name}:post`, async run(data) {
    const callId = data.toolCall.callId;
    const permission = calls.get(data.context)?.get(callId);
    calls.get(data.context)?.delete(callId);
    if (!permission?.executed) return behavior.throwException({ framework: "norma", callId, stage: "post" });
    let decision: Decision | undefined;
    try {
      if (!permission.client.initialized || permission.client.connectionRevision !== permission.session) throw new Error("Norma session changed before post check");
      decision = await permission.client.checks.check({ stage: "post", step: {
        type: "tool", name: options.name, input: { args: [permission.input] }, output: jsonValueSchema.parse(data.output),
        context: { ...(permission.requestContext.context ?? {}), framework: "openai-agents", agent_name: permission.agentName, tool_call_id: callId },
      }, targetType: permission.requestContext.targetType, targetId: permission.requestContext.targetId,
      traceId: permission.requestContext.traceId, spanId: permission.requestContext.spanId });
      if (!permission.client.initialized || permission.client.connectionRevision !== permission.session) throw new Error("Norma session changed during post check");
      if (options.isActive && !options.isActive()) throw new Error("Configured Norma adapter closed after the action ran.");
      enforceCheckpointDecision(decision, "post");
      await options.onRecord?.({ agentName: permission.agentName, frameworkAgentName: permission.frameworkAgentName, toolName: options.name, callId, stage: "post", status: "allowed", decision });
      if (!permission.client.initialized || permission.client.connectionRevision !== permission.session) throw new Error("Norma session changed during post recording");
      return behavior.allow();
    } catch (error) {
      await options.onRecord?.({ agentName: permission.agentName, frameworkAgentName: permission.frameworkAgentName, toolName: options.name, callId, stage: "post", status: error instanceof RuleSteerError ? "steered" : error instanceof RuleViolationError ? "denied" : "error", decision });
      return behavior.throwException({ framework: "norma", callId, stage: "post" });
    }
  }});
  return tool<NormaToolParameters, Context, JsonValue>({
    name: options.name, description: options.description, parameters: options.parameters, strict: true,
    needsApproval: options.needsApproval, errorFunction: null,
    inputGuardrails: [...(options.inputGuardrails ?? []), input],
    outputGuardrails: [output, ...(options.outputGuardrails ?? [])],
    async execute(args, context, details) {
      const callId = details?.toolCall?.callId;
      const permission = context && callId ? calls.get(context)?.get(callId) : undefined;
      if (!context || !permission || permission.executed) throw new Error("Missing Norma execution permission; use the Agents Runner");
      if (!permission.client.initialized || permission.client.connectionRevision !== permission.session) throw new Error("Norma session changed before execution");
      if (options.isActive && !options.isActive()) throw new Error("Configured Norma adapter is closed; execution was blocked.");
      const used = consumed.get(context) ?? new Set<string>();
      consumed.set(context, used);
      if (used.has(callId!)) throw new Error("Tool call was already executed");
      used.add(callId!);
      permission.executed = true;
      details?.signal?.throwIfAborted();
      const snapshot = objectSnapshot(args);
      if (JSON.stringify(snapshot) !== permission.fingerprint) throw new Error("Tool arguments changed after Norma check");
      const started = Date.now();
      try {
        const result = await options.execute(snapshot, context, details?.signal);
        if (permission.action) recordDecoratedAction({ kind: permission.action.kind, nativeTool: options.name, status: "succeeded",
          target: permission.action.target, command: permission.action.command, durationMs: Math.max(0, Date.now() - started) });
        return result;
      } catch (error) {
        calls.get(context)?.delete(callId!);
        if (permission.action) recordDecoratedAction({ kind: permission.action.kind, nativeTool: options.name,
          status: details?.signal?.aborted ? "unknown" : "failed", target: permission.action.target,
          command: permission.action.command, outputTail: error instanceof Error ? error.name : "error",
          durationMs: Math.max(0, Date.now() - started) });
        throw error;
      }
    },
  });
}
