// Modified for Norma.
/** Retained decisions and separately attributed effects; never executes a tool. */
import * as z from "zod/v4";
import { NormaAPIError } from "../api-error";
export { NormaAPIError } from "../api-error";
import { copyCheckpointArguments } from "../checkpoint-arguments";
import { jsonObjectSchema } from "../json-contract";
import type { NormaSDK } from "../generated/sdk/sdk";
import { extractSecurity, resolveGlobalSecurity } from "../generated/lib/security";
import { RuleDefinitionOutput$inboundSchema } from "../generated/models/rule-definition-output";
import { CheckResponse$inboundSchema } from "../generated/models/check-response";
import { ControlReceipt$inboundSchema } from "../generated/models/control-evidence";
import type { ControlReceipt } from "../generated/models/control-evidence";
import { decisionFromWire, ruleDefinitionFromWire } from "./public-adapters";
import type { Decision, RuleDefinition } from "../public-api";
import type { JsonObject } from "../types";
import type { SemanticAnswer } from "./semantic-check";
import { SemanticAnswerSchema } from "./semantic-check";

const text = (max: number) => z.string().min(1).max(max);
const optionalText = z.string().nullable().optional();
const strings = z.array(z.string());
const jsonObject = jsonObjectSchema;
const target = { targetType: text(255).optional(), targetId: text(255).optional() };
const identitySchema = z.strictObject({ agentName: text(255).min(10), ...target })
  .refine((value) => Boolean(value.targetType) === Boolean(value.targetId), "Both target fields are required");

const proposalStep = z.strictObject({
  type: z.literal("tool"), name: z.string().min(1), input: jsonObjectSchema,
  output: z.null().optional(), groundTruth: z.null().optional(),
  context: z.optional(z.nullable(jsonObject)), tools: z.array(jsonObject).nullable().optional(),
}).refine((step) => !step.context || !("norma_operation_evidence" in step.context), "Reserved operation evidence context");
const operationEvidence = z.strictObject({ id: text(128), summary: text(2000), source: text(255) });
const createSchema = z.strictObject({
  operationKey: text(128), step: proposalStep,
  errorPolicy: z.literal("block").default("block"),
  unresolvedPolicy: z.enum(["allow", "block"]).default("block"),
  evidence: z.array(operationEvidence).max(30).default([]),
});
export interface CreateOperation {
  operationKey: string; step: {type: "tool"; name: string; input: JsonObject; output?: null; groundTruth?: null; context?: JsonObject | null; tools?: JsonObject[] | null};
  errorPolicy?: "block"; unresolvedPolicy?: "allow" | "block"; evidence?: {id: string; summary: string; source: string}[];
}
const observeSchema = z.strictObject({
  eventKey: text(128), kind: z.enum(["attempted", "succeeded", "failed", "unknown"]),
  summary: text(2000), externalReference: text(255).nullable().optional(),
});
export interface ObserveOperation {eventKey: string; kind: "attempted" | "succeeded" | "failed" | "unknown"; summary: string; externalReference?: string | null}
export interface OperationRequestOptions { signal?: AbortSignal; timeoutMs?: number }

// Only known contract fields are remapped. Proposal/evidence JSON is never recursively renamed.
const retainedStep = z.object({
  type: z.literal("tool"), name: z.string().min(1), input: jsonObjectSchema, output: z.null().optional(),
  ground_truth: z.null().optional(), context: z.optional(z.nullable(jsonObject)),
  tools: z.array(jsonObject).nullable().optional(),
}).transform(({ ground_truth, ...rest }) => ({ ...rest, groundTruth: ground_truth }));
const retainedRequest = z.object({
  agent_name: z.string(), operation_key: z.string(), step: retainedStep, stage: z.literal("pre"),
  target_type: optionalText, target_id: optionalText,
  error_policy: z.enum(["allow", "block"]), unresolved_policy: z.enum(["allow", "block"]),
  evidence: z.array(operationEvidence),
}).transform(({ agent_name, operation_key, target_type, target_id, error_policy, unresolved_policy, ...rest }) => ({
  ...rest, agentName: agent_name, operationKey: operation_key, targetType: target_type,
  targetId: target_id, errorPolicy: error_policy, unresolvedPolicy: unresolved_policy,
}));
const rawMatch = z.object({
  action: z.enum(["deny", "steer", "observe"]), rule_id: z.int(), rule_name: z.string(),
  effective_action: z.enum(["deny", "steer", "observe"]).nullable().optional(),
  result: z.object({ matched: z.boolean(), confidence: z.number().min(0).max(1) }).passthrough(),
}).passthrough();
const rawMatches = z.array(rawMatch).nullable().optional();
const retainedCheck = z.object({
  allowed: z.boolean(), confidence: z.number().min(0).max(1),
  matches: rawMatches, non_matches: rawMatches, errors: rawMatches, unresolved: rawMatches,
}).passthrough().transform((value) => CheckResponse$inboundSchema.parse(value));
const ruleSnapshot = z.object({
  rule_id: z.int(), name: z.string(), version: z.int().nullable().optional(),
  definition: RuleDefinitionOutput$inboundSchema, definition_fingerprint: z.string(),
  disposition: z.enum(["evaluated", "cancelled", "skipped", "unknown"]),
  agent_generation: optionalText, control_resolution: z.optional(z.nullable(ControlReceipt$inboundSchema)),
}).transform(({ rule_id, definition_fingerprint, agent_generation, control_resolution, ...rest }) => ({
  ...rest, ruleId: rule_id, definitionFingerprint: definition_fingerprint,
  agentGeneration: agent_generation, controlResolution: control_resolution,
}));
const observation = z.object({
  id: z.string(), event_key: z.string(), kind: z.enum(["attempted", "succeeded", "failed", "unknown"]),
  summary: z.string(), external_reference: optionalText, recorded_at: z.string(),
  source: z.literal("caller_reported"),
}).transform(({ event_key, external_reference, recorded_at, ...rest }) => ({
  ...rest, eventKey: event_key, externalReference: external_reference, recordedAt: recorded_at,
}));
const evidenceReference = z.object({
  id: z.string(), evidence_id: z.string(), origin: z.enum(["caller_asserted", "runtime_observed", "source_verified"]),
  source_id: optionalText, source_version: optionalText, subject_id: z.string(), subject_version: z.string(),
  correlation_id: optionalText, related_subject_ids: strings, observed_at: optionalText,
  received_at: z.string(), fingerprint: z.string(),
  status: z.enum(["available", "missing", "stale", "untrusted", "conflicting", "unavailable"]),
}).transform(({ evidence_id, source_id, source_version, subject_id, subject_version, correlation_id,
  related_subject_ids, observed_at, received_at, ...rest }) => ({
  ...rest, evidenceId: evidence_id, sourceId: source_id, sourceVersion: source_version,
  subjectId: subject_id, subjectVersion: subject_version, correlationId: correlation_id,
  relatedSubjectIds: related_subject_ids, observedAt: observed_at, receivedAt: received_at,
}));
const criterion = z.object({
  criterion_id: z.string(), status: z.enum(["satisfied", "unsatisfied", "stale", "unknown", "conflicting"]),
  basis: z.enum(["exact_check", "semantic_inference"]), reason: z.string(), evidence_ids: strings,
  observed_value_fingerprint: optionalText,
}).transform(({ criterion_id, evidence_ids, observed_value_fingerprint, ...rest }) => ({
  ...rest, criterionId: criterion_id, evidenceIds: evidence_ids, observedValueFingerprint: observed_value_fingerprint,
}));
const usage = z.object({ input_tokens: z.int().min(0).optional(), output_tokens: z.int().min(0).optional() })
  .transform(({ input_tokens, output_tokens }) => ({ inputTokens: input_tokens, outputTokens: output_tokens }));
const judgment = z.object({
  check_release: z.string(), question_fingerprint: z.string(), answers: z.record(z.string(), SemanticAnswerSchema),
  rule_outcomes: z.record(z.string(), z.enum(["matched", "not_matched", "unresolved"])),
  usage: usage.nullable().optional(), context_evidence_ids: strings,
}).transform(({ check_release, question_fingerprint, rule_outcomes, context_evidence_ids, ...rest }) => ({
  ...rest, checkRelease: check_release, questionFingerprint: question_fingerprint,
  ruleOutcomes: rule_outcomes, contextEvidenceIds: context_evidence_ids,
}));
const assessment = z.object({
  revision: z.int(), subject_version: z.string(), definition_fingerprint: z.string(),
  previous_assessment_fingerprint: optionalText, fingerprint: z.string(),
  outcome: z.enum(["success", "failed", "partial", "stale", "unknown", "conflicting"]),
  claim_scope: z.enum(["submitted_artifact", "observed_state", "correlated_external_effect", "related_sequence"]),
  criteria: z.array(criterion), observed_facts: z.array(evidenceReference), inferred_judgment: judgment.nullable().optional(),
  satisfied_criteria: strings, unsatisfied_criteria: strings, assessed_at: z.string(), valid_until: optionalText, note: optionalText,
}).transform(({ subject_version, definition_fingerprint, previous_assessment_fingerprint, claim_scope,
  observed_facts, inferred_judgment, satisfied_criteria, unsatisfied_criteria, assessed_at, valid_until, ...rest }) => ({
  ...rest, subjectVersion: subject_version, definitionFingerprint: definition_fingerprint,
  previousAssessmentFingerprint: previous_assessment_fingerprint, claimScope: claim_scope,
  observedFacts: observed_facts, inferredJudgment: inferred_judgment, satisfiedCriteria: satisfied_criteria,
  unsatisfiedCriteria: unsatisfied_criteria, assessedAt: assessed_at, validUntil: valid_until,
}));
const verification = z.object({
  session_id: z.string(), revision: z.int(), claim: z.literal("postcondition_assessed"),
  assessment, linked_at: z.string(),
}).transform(({ session_id, linked_at, ...rest }) => ({ ...rest, sessionId: session_id, linkedAt: linked_at }));
const operationRecordSchema = z.object({
  id: text(255), operation_key: text(128), agent_name: z.string(), target_type: optionalText, target_id: optionalText,
  control_contract: z.optional(z.nullable(z.literal("rule-controls-v1"))), agent_generation: optionalText,
  created_at: z.string(), request_fingerprint: z.string(), request: retainedRequest,
  decision: z.object({ outcome: z.enum(["allow", "deny", "steer"]), reason: z.string(), check: retainedCheck }),
  rules: z.array(ruleSnapshot), observations: z.array(observation), verifications: z.array(verification),
  discrepancy: z.enum(["none", "reported_execution_after_denial"]),
  current_outcome: z.enum(["not_observed", "attempted", "succeeded", "failed", "unknown", "conflicting"]),
}).transform(({ operation_key, agent_name, target_type, target_id, created_at, request_fingerprint, current_outcome,
  control_contract, agent_generation, ...rest }) => ({
  ...rest, operationKey: operation_key, agentName: agent_name, targetType: target_type, targetId: target_id,
  createdAt: created_at, requestFingerprint: request_fingerprint, currentOutcome: current_outcome,
  controlContract: control_contract, agentGeneration: agent_generation,
}));
type WireOperationRecord = z.output<typeof operationRecordSchema>;
export interface OperationObservation extends ObserveOperation {id: string; recordedAt: string; source: "caller_reported"}
export interface OperationEvidenceReference {
  id: string; evidenceId: string; origin: "caller_asserted" | "runtime_observed" | "source_verified";
  sourceId?: string | null; sourceVersion?: string | null; subjectId: string; subjectVersion: string;
  correlationId?: string | null; relatedSubjectIds: string[]; observedAt?: string | null;
  receivedAt: string; fingerprint: string; status: "available" | "missing" | "stale" | "untrusted" | "conflicting" | "unavailable";
}
export interface OperationVerification {
  sessionId: string; revision: number; claim: "postcondition_assessed"; linkedAt: string;
  assessment: {
    revision: number; subjectVersion: string; definitionFingerprint: string; previousAssessmentFingerprint?: string | null;
    fingerprint: string; outcome: "success" | "failed" | "partial" | "stale" | "unknown" | "conflicting";
    claimScope: "submitted_artifact" | "observed_state" | "correlated_external_effect" | "related_sequence";
    criteria: {criterionId: string; status: "satisfied" | "unsatisfied" | "stale" | "unknown" | "conflicting";
      basis: "exact_check" | "semantic_inference"; reason: string; evidenceIds: string[]; observedValueFingerprint?: string | null}[];
    observedFacts: OperationEvidenceReference[];
    inferredJudgment?: {checkRelease: string; questionFingerprint: string; answers: Record<string, SemanticAnswer>;
      ruleOutcomes: Record<string, "matched" | "not_matched" | "unresolved">; usage?: {inputTokens?: number; outputTokens?: number} | null; contextEvidenceIds: string[]} | null;
    satisfiedCriteria: string[]; unsatisfiedCriteria: string[]; assessedAt: string; validUntil?: string | null; note?: string | null;
  };
}
export interface OperationRecord {
  id: string; operationKey: string; agentName: string; targetType?: string | null; targetId?: string | null;
  controlContract?: "rule-controls-v1" | null; agentGeneration?: string | null;
  createdAt: string; requestFingerprint: string;
  request: {agentName: string; operationKey: string; stage: "pre"; targetType?: string | null; targetId?: string | null;
    step: CreateOperation["step"]; errorPolicy: "allow" | "block"; unresolvedPolicy: "allow" | "block"; evidence: {id: string; summary: string; source: string}[]};
  decision: Decision & {outcome: "allow" | "deny" | "steer"};
  rules: {ruleId: number; name: string; version?: number | null; definition: RuleDefinition | null;
    status: "hosted" | "unsupported"; definitionFingerprint: string; disposition: "evaluated" | "cancelled" | "skipped" | "unknown";
    agentGeneration?: string | null; controlResolution?: ControlReceipt | null}[];
  observations: OperationObservation[]; verifications: OperationVerification[];
  discrepancy: "none" | "reported_execution_after_denial";
  currentOutcome: "not_observed" | "attempted" | "succeeded" | "failed" | "unknown" | "conflicting";
}
function publicRecord(record: WireOperationRecord): OperationRecord {
  const {rules, decision, ...rest} = record;
  const normalized = decisionFromWire(decision.check);
  return {...rest, decision: {...normalized, allowed: decision.outcome === "allow" && !normalized.checks.some(check => check.mode !== "observe" && (check.status === "error" || check.result.error != null)), outcome: decision.outcome, reason: decision.reason},
    rules: rules.map(({ruleId, definition, ...item}) => {
      const publicDefinition = ruleDefinitionFromWire(definition);
      return {...item, ruleId: ruleId, definition: publicDefinition, status: publicDefinition ? "hosted" : "unsupported"};
    })};
}
export interface OperationsApi {
  create(request: CreateOperation, options?: OperationRequestOptions): Promise<OperationRecord>;
  get(operationId: string, options?: OperationRequestOptions): Promise<OperationRecord>;
  list(options?: {limit?: number}, requestOptions?: OperationRequestOptions): Promise<OperationRecord[]>;
  observe(operationId: string, request: ObserveOperation, options?: OperationRequestOptions): Promise<OperationRecord>;
  linkVerification(operationId: string, verificationSessionId: string, options?: OperationRequestOptions): Promise<OperationRecord>;
}

/** Uses the initialized agent, target, security and HTTP transport for every request. */
export class HostedOperationsApi implements OperationsApi {
  private readonly identity: z.output<typeof identitySchema>;
  constructor(private readonly sdk: NormaSDK, identity: z.input<typeof identitySchema>) {
    this.identity = identitySchema.parse(identity);
  }

  async create(request: CreateOperation, options?: OperationRequestOptions): Promise<OperationRecord> {
    const input = createSchema.parse(copyCheckpointArguments([request], false)[0]);
    const { groundTruth, ...step } = input.step;
    const payload = { ...this.scope(), operation_key: input.operationKey, stage: "pre", execution_mode: "hosted",
      step: { ...step, ...(groundTruth === undefined ? {} : { ground_truth: groundTruth }) },
      error_policy: input.errorPolicy, unresolved_policy: input.unresolvedPolicy, evidence: input.evidence };
    const body = JSON.stringify(payload);
    if (new TextEncoder().encode(body).length > 65536) throw new TypeError("Operation proposal exceeds 65536 bytes");
    const record = this.read(await this.request("POST", "/api/v1/operations", body, options));
    if (record.decision.check.executionContract !== "hosted-v1") {
      throw new Error("Hosted operation requires hosted-v1 execution proof.");
    }
    if (record.rules.some((rule) => rule.definition.enabled && ruleDefinitionFromWire(rule.definition) === null)) {
      throw new Error("Hosted Norma operations cannot accept an SDK-local rule snapshot.");
    }
    const expected = { ...input, step: { ...input.step, output: null, groundTruth: null,
      context: input.step.context ?? null, tools: input.step.tools ?? null } };
    const actual = { operationKey: record.request.operationKey, errorPolicy: record.request.errorPolicy,
      unresolvedPolicy: record.request.unresolvedPolicy, evidence: record.request.evidence,
      step: { ...record.request.step, output: null, groundTruth: null,
        context: record.request.step.context ?? null, tools: record.request.step.tools ?? null } };
    if (!sameJSON(expected, actual)) throw new Error("Operation receipt does not match the requested proposal");
    return publicRecord(record);
  }

  async get(operationId: string, options?: OperationRequestOptions): Promise<OperationRecord> {
    return publicRecord(this.read(await this.request("GET", this.path(operationId), undefined, options), operationId));
  }

  async list(options: { limit?: number } = {}, requestOptions?: OperationRequestOptions): Promise<OperationRecord[]> {
    const { limit } = z.strictObject({ limit: z.int().min(1).max(100).default(50) }).parse(options);
    const body = await this.request("GET", "/api/v1/operations", undefined, requestOptions, { limit: String(limit) });
    const result = z.object({ operations: z.array(z.unknown()) }).parse(body);
    return result.operations.map((record) => publicRecord(this.read(record)));
  }

  async observe(operationId: string, request: ObserveOperation, options?: OperationRequestOptions): Promise<OperationRecord> {
    const input = observeSchema.parse(copyCheckpointArguments([request], false)[0]);
    const payload = { ...this.scope(), event_key: input.eventKey, kind: input.kind,
      summary: input.summary, external_reference: input.externalReference ?? null };
    const record = this.read(await this.request("POST", `${this.path(operationId)}/observations`, JSON.stringify(payload), options), operationId);
    if (!record.observations.some((report) => report.eventKey === input.eventKey && report.kind === input.kind
      && report.summary === input.summary && (report.externalReference ?? null) === (input.externalReference ?? null)
      && report.source === "caller_reported")) throw new Error("Operation receipt does not acknowledge the reported effect");
    return publicRecord(record);
  }

  async linkVerification(operationId: string, verificationSessionId: string, options?: OperationRequestOptions): Promise<OperationRecord> {
    const payload = { ...this.scope(), verification_session_id: text(128).parse(verificationSessionId) };
    const record = this.read(await this.request("POST", `${this.path(operationId)}/verification`, JSON.stringify(payload), options), operationId);
    if (!record.verifications.some((linked) => linked.sessionId === verificationSessionId)) {
      throw new Error("Operation receipt does not acknowledge the linked verification");
    }
    return publicRecord(record);
  }

  private path(id: string): string {
    const safe = text(255).refine((value) => value !== "." && value !== "..", "Invalid operation ID").parse(id);
    return `/api/v1/operations/${encodeURIComponent(safe)}`;
  }
  private scope(): { agent_name: string; target_type?: string; target_id?: string } {
    return { agent_name: this.identity.agentName,
      ...(this.identity.targetType === undefined ? {} : { target_type: this.identity.targetType, target_id: this.identity.targetId }) };
  }
  private read(value: unknown, id?: string): WireOperationRecord {
    const record = operationRecordSchema.parse(value);
    const expected = this.identity.agentName.trim().toLowerCase();
    if (record.agentName !== expected || record.request.agentName !== expected
      || (record.targetType ?? null) !== (this.identity.targetType ?? null)
      || (record.targetId ?? null) !== (this.identity.targetId ?? null)
      || (record.request.targetType ?? null) !== (this.identity.targetType ?? null)
      || (record.request.targetId ?? null) !== (this.identity.targetId ?? null)
      || record.operationKey !== record.request.operationKey || (id !== undefined && record.id !== id)) {
      throw new Error("Operation receipt does not match the requested identity");
    }
    if (record.decision.outcome === "allow") {
      const evaluated = record.decision.check;
      const matches = (evaluated.matches ?? []).filter(item => item.mode !== "observe");
      const unresolved = (evaluated.unresolved ?? []).filter(item => item.mode !== "observe");
      const actions = [...matches.map((match) => match.action),
        ...unresolved.map((match) => match.effectiveAction ?? match.action)];
      const hasErrors = [...matches, ...(evaluated.nonMatches ?? []), ...unresolved, ...(evaluated.errors ?? [])].some(item => item.mode !== "observe" && item.result.error != null) || Boolean(evaluated.errors?.some(item => item.mode !== "observe"));
      const errorsAllowed = record.request.errorPolicy === "allow" && hasErrors;
      const unresolvedAllowed = record.request.unresolvedPolicy === "allow" && unresolved.length > 0;
      const observed = matches.some((match) => match.action === "observe" && match.result.matched === true && !match.result.error);
      if (actions.some((action) => action === "deny" || action === "steer")
        || (hasErrors && record.request.errorPolicy === "block")
        || (unresolved.length && record.request.unresolvedPolicy === "block")
        || (!evaluated.allowed && !observed && !errorsAllowed && !unresolvedAllowed)) {
        throw new Error("Operation allow receipt contradicts its retained check");
      }
    }
    return record;
  }

  private async request(method: "GET" | "POST", path: string, body?: string,
    options: OperationRequestOptions = {}, query: Record<string, string> = {}): Promise<unknown> {
    // Snapshot and serialize before awaiting a dynamic key; never retry an ambiguous mutation.
    const requestOptions = z.strictObject({
      timeoutMs: z.number().finite().nonnegative().optional(),
      signal: z.instanceof(AbortSignal).optional(),
    }).parse(options);
    requestOptions.signal?.throwIfAborted();
    const key = await extractSecurity(this.sdk._options.apiKeyHeader);
    requestOptions.signal?.throwIfAborted();
    const security = resolveGlobalSecurity(key == null ? {} : { apiKeyHeader: key });
    const context = { options: this.sdk._options, baseURL: this.sdk._baseURL ?? "",
      operationID: `norma_operations_${method.toLowerCase()}`, oAuth2Scopes: null,
      resolvedSecurity: security, securitySource: this.sdk._options.apiKeyHeader,
      retryConfig: { strategy: "none" as const } };
    const queryString = method === "GET" ? new URLSearchParams({ ...this.scope(), ...query }).toString() : undefined;
    const request = this.sdk._createRequest(context, { method, path, body, query: queryString,
      security, headers: { "Content-Type": "application/json", Accept: "application/json" },
      userAgent: this.sdk._options.userAgent, timeoutMs: requestOptions.timeoutMs ?? this.sdk._options.timeoutMs }, requestOptions);
    if (!request.ok) throw request.error;
    const result = await this.sdk._do(request.value, { context, errorCodes: ["4XX", "5XX"],
      retryConfig: { strategy: "none" }, retryCodes: [] });
    if (!result.ok) throw result.error;
    if (result.value.status !== 200) {
      const body = await result.value.text().catch(() => "");
      throw NormaAPIError.fromResponse(result.value, body, request.value.url);
    }
    return result.value.json();
  }
}

function sameJSON(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const a = Object.entries(left).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0);
  const b = Object.entries(right).sort(([x], [y]) => x < y ? -1 : x > y ? 1 : 0);
  return a.length === b.length && a.every(([key, value], index) => key === b[index][0] && sameJSON(value, b[index][1]));
}
