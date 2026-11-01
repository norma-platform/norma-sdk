// Modified for Norma.
import { recordDecoratedAction } from "./finish";
import type { NormaAction } from "./checkpoint";
import { operationsFor, type NormaClient } from "./client";
import type { OperationRecord } from "./internal/operations";
import type { InstrumentationOperation } from "./instrumentation";
import { CheckError } from "./errors";
import { enforceCheckpointDecision } from "./checkpoint";
import type { JsonObject, JsonValue } from "./types";
import { copyCheckpointArguments } from "./checkpoint-arguments";

export interface OperationInvocation {
  args: unknown[];
  kwargs: Record<string, never>;
  result?: unknown;
  error?: unknown;
  context: JsonObject;
  agent_name: string;
}
export type OperationValueResolver = (invocation: OperationInvocation) => Promise<JsonValue> | JsonValue;
export interface CompiledOperationPlan {
  config: InstrumentationOperation;
  operationName: string;
  operationKey: OperationValueResolver;
  input?: OperationValueResolver;
  context?: OperationValueResolver;
  evidence?: OperationValueResolver;
  tools?: OperationValueResolver;
  attempted?: CompiledObservation;
  succeeded?: CompiledObservation;
  failed?: CompiledObservation;
  verificationSessionId?: OperationValueResolver;
  action?: (...args: unknown[]) => NormaAction | Promise<NormaAction>;
  onRecord?: { owner: object; callback: (...args: unknown[]) => unknown };
}
export interface CompiledObservation {
  eventKey: OperationValueResolver;
  summary: OperationValueResolver;
  externalReference?: OperationValueResolver;
}

const objectSnapshot = (value: JsonValue, label: string): JsonObject => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} must resolve to a JSON object.`);
  return value as JsonObject;
};
const stringValue = (value: JsonValue, label: string): string => {
  if (typeof value !== "string" || !value.trim()) throw new TypeError(`${label} must resolve to a nonblank string.`);
  return value;
};
const objectArray = (value: JsonValue, label: string): JsonObject[] => {
  if (!Array.isArray(value) || value.some(item => !item || typeof item !== "object" || Array.isArray(item))) {
    throw new TypeError(`${label} must resolve to a JSON array of objects.`);
  }
  return value as JsonObject[];
};
const staticActionValue = (value: unknown, label: string): string | null | undefined => {
  if (value === undefined || value === null || typeof value === "string") return value as string | null | undefined;
  throw new TypeError(`${label} must be resolved before action recording.`);
};

/** Wrap one declared business operation around Norma's retained pre-decision API. */
export function wrapConfiguredOperation(
  fn: (...args: unknown[]) => unknown,
  plan: CompiledOperationPlan,
  client: NormaClient,
  getContext: () => JsonObject,
  getTargetScope: () => { targetType?: string; targetId?: string } | undefined,
  isActive: () => boolean,
): (...args: unknown[]) => Promise<unknown> {
  const invokeCallback = async (record: OperationRecord, stage: string, invocation: OperationInvocation): Promise<void> => {
    if (plan.onRecord) await plan.onRecord.callback.call(plan.onRecord.owner, { ...invocation, stage, operation: record });
  };
  const report = async (record: OperationRecord, observation: CompiledObservation | undefined,
    kind: "attempted" | "succeeded" | "failed", invocation: OperationInvocation): Promise<OperationRecord> => {
    if (!observation) return record;
    const eventKey = stringValue(await observation.eventKey(invocation), `operations.${plan.operationName}.observations.${kind}.event_key`);
    const summary = stringValue(await observation.summary(invocation), `operations.${plan.operationName}.observations.${kind}.summary`);
    const externalReference = observation.externalReference
      ? stringValue(await observation.externalReference(invocation), `operations.${plan.operationName}.observations.${kind}.external_reference`)
      : undefined;
    const updated = await operationsFor(client, getTargetScope()).observe(record.id, { eventKey, kind, summary, ...(externalReference === undefined ? {} : { externalReference }) });
    await invokeCallback(updated, kind, invocation);
    return updated;
  };
  return async function (this: unknown, ...args: unknown[]): Promise<unknown> {
    const revision = client.connectionRevision;
    const assertCurrent = (): void => {
      if (!isActive() || !client.initialized || client.connectionRevision !== revision) {
        throw new CheckError({ stage: "pre", message: "Configured Norma identity changed during operation evaluation; execution was blocked." });
      }
    };
    assertCurrent();
    const approvedArgs = copyCheckpointArguments(args, true) as unknown[];
    const initial: OperationInvocation = { args: approvedArgs, kwargs: {}, context: getContext(), agent_name: client.config!.agentName };
    const operationKey = stringValue(await plan.operationKey(initial), `operations.${plan.operationName}.operation_key`);
    const input = plan.input
      ? objectSnapshot(await plan.input(initial), `operations.${plan.operationName}.input`)
      : { args: approvedArgs as unknown as JsonValue[], kwargs: {} } as unknown as JsonObject;
    const context = plan.context ? objectSnapshot(await plan.context(initial), `operations.${plan.operationName}.context`) : undefined;
    const evidence = plan.evidence ? objectArray(await plan.evidence(initial), `operations.${plan.operationName}.evidence`) as unknown as {id: string; summary: string; source: string}[] : undefined;
    const tools = plan.tools ? objectArray(await plan.tools(initial), `operations.${plan.operationName}.tools`) : undefined;
    assertCurrent();
    const target = getTargetScope();
    const operationApi = operationsFor(client, target);
    const proposal = await operationApi.create({
      operationKey,
      step: { type: "tool", name: plan.operationName, input, ...(context === undefined ? {} : { context }), ...(tools === undefined ? {} : { tools }) },
      ...(target?.targetType === undefined ? {} : { targetType: target.targetType, targetId: target.targetId }),
      unresolvedPolicy: plan.config.unresolved_policy ?? "block",
      ...(evidence === undefined ? {} : { evidence: evidence as unknown as {id: string; summary: string; source: string}[] }),
    });
    assertCurrent();
    await invokeCallback(proposal, "proposed", initial);
    assertCurrent();
    try {
      enforceCheckpointDecision(proposal.decision, "pre");
    } catch (error) {
      if (plan.action) {
        const action = await plan.action(...approvedArgs);
        assertCurrent();
        recordDecoratedAction({ kind: action.kind, nativeTool: plan.operationName, status: "denied",
          target: staticActionValue(action.target, "operation action target"), command: staticActionValue(action.command, "operation action command"), outputTail: error instanceof Error ? error.name : "denied" });
      }
      throw error;
    }
    let record = await report(proposal, plan.attempted, "attempted", initial);
    assertCurrent();
    let action: NormaAction | undefined;
    if (plan.action) action = await plan.action(...approvedArgs);
    assertCurrent();
    let result: unknown;
    try {
      const pending = fn.apply(this, approvedArgs);
      if (pending === null || (typeof pending !== "object" && typeof pending !== "function")
          || typeof (pending as { then?: unknown }).then !== "function") {
        throw new TypeError(`Operation target '${plan.operationName}' declared returns: "promise" but returned a non-Promise value.`);
      }
      result = await pending;
    } catch (error) {
      const after: OperationInvocation = { ...initial, error };
      try {
        record = await report(record, plan.failed, "failed", after);
        if (action) recordDecoratedAction({ kind: action.kind, nativeTool: plan.operationName, status: "failed", target: staticActionValue(action.target, "operation action target"), command: staticActionValue(action.command, "operation action command"),
          outputTail: error instanceof Error ? error.name : "error" });
      } catch (observationError) {
        if (error && (typeof error === "object" || typeof error === "function")) {
          try { Object.defineProperty(error, "normaObservationError", { value: observationError, configurable: true }); } catch { /* preserve the business error */ }
        }
      }
      throw error;
    }
    const after: OperationInvocation = { ...initial, result };
    assertCurrent();
    record = await report(record, plan.succeeded, "succeeded", after);
    if (plan.verificationSessionId) {
      const sessionId = stringValue(await plan.verificationSessionId(after), `operations.${plan.operationName}.verification_session_id`);
      assertCurrent();
      record = await operationApi.linkVerification(record.id, sessionId);
      await invokeCallback(record, "verification", after);
    }
    if (action) recordDecoratedAction({ kind: action.kind, nativeTool: plan.operationName, status: "succeeded", target: staticActionValue(action.target, "operation action target"), command: staticActionValue(action.command, "operation action command") });
    return result;
  };
}
