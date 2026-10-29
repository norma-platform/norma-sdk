// Modified for Norma.
/** Public Rules contract; native runtime JSON is validated by internal codecs. */
import type { JsonObject, JsonValue } from "./types";
import type { SemanticObservation, RecoveryFeedback } from "./internal/semantic-check";
import type { StepSchema } from "./generated/models/step-schema";
import type { ControlEvidenceSnapshot, ControlReceipt } from "./generated/models/control-evidence";

export interface RequestOptions { signal?: AbortSignal; timeoutMs?: number }
export type RuleDecision = "deny" | "steer" | "observe";
export interface RuleScope { stepTypes?: string[] | null; stepNames?: string[] | null; stepNameRegex?: string | null; stages?: ("pre" | "post")[] | null }
export interface RuleAction { decision: RuleDecision; steeringContext?: {message: string} | null }
export interface RuleCondition {
  and?: RuleCondition[] | null; or?: RuleCondition[] | null; not?: RuleCondition | null;
  selector?: {path?: string | null} | null; evaluator?: {name: string; config: JsonObject} | null;
}
export interface RuleDefinition {
  mode?: "enforce" | "observe";
  execution?: "server"; enabled?: boolean; action: RuleAction; condition: RuleCondition;
  description?: string | null; scope?: RuleScope; tags?: string[];
}
/** Hosted Rules are available; unsupported definitions remain visible without becoming authorable. */
export interface Rule { id: number; name: string; definition: RuleDefinition | null; status: "hosted" | "unsupported" }
export interface RuleResult {
  matched: boolean; confidence: number; outcome?: "matched" | "not_matched" | "unresolved" | null;
  error?: string | null; message?: string | null; metadata?: JsonObject | null;
  semanticObservation?: SemanticObservation | null; failedConditionIds?: string[];
  unresolvedEvidenceIds?: string[]; unresolvedAction?: RuleDecision | null;
}
export interface RuleMatch {
  mode?: "enforce" | "observe";
  ruleId: number; ruleName: string; checkId?: string; action: RuleDecision;
  effectiveAction?: RuleDecision | null; result: RuleResult; steeringContext?: {message: string} | null;
  status: "matched" | "not_matched" | "unresolved" | "error";
}
export type CheckResult = RuleMatch;
export interface CheckRequest {
  stage: "pre" | "post";
  step: { type: string; name: string; input: JsonValue; output?: JsonValue; groundTruth?: JsonValue;
    context?: JsonObject | null; tools?: JsonObject[] | null };
  targetType?: string | null; targetId?: string | null;
}
export interface Decision {
  allowed: boolean; reason: string | null; checks: CheckResult[]; recoveryFeedback?: RecoveryFeedback[] | null;
  controlContract?: "rule-controls-v1" | null; agentGeneration?: string | null;
  controlResolutions?: ControlReceipt[] | null; controlEvidence?: ControlEvidenceSnapshot[] | null;
}
export type CheckResponse = Decision;
export interface ChecksApi { check(request: CheckRequest, options?: RequestOptions): Promise<Decision> }

export interface Agent { agentName: string; agentDescription?: string | null; agentMetadata?: JsonObject | null;
  agentVersion?: string | null; agentCreatedAt?: string | null; agentUpdatedAt?: string | null }
export interface EvaluatorSchema { name: string; description?: string | null; configSchema?: JsonObject }
export interface Pagination { hasMore: boolean; limit: number; total: number; nextCursor?: string | null }
export interface StepKey { type: string; name: string }
export interface AgentOverwriteChanges {
  metadataChanged: boolean; evaluatorsAdded?: string[]; evaluatorsRemoved?: string[]; evaluatorsUpdated?: string[];
  stepsAdded?: StepKey[]; stepsRemoved?: StepKey[]; stepsUpdated?: StepKey[];
  evaluatorRemovals?: {name: string; ruleIds?: number[]; ruleNames?: string[]; referencedByActiveRules: boolean}[];
}
export interface AgentsApi {
  list(request?: {cursor?: string | null; limit?: number; name?: string | null}, options?: RequestOptions): Promise<{
    agents: {agentName: string; activeRulesCount: number; createdAt?: string | null; evaluatorCount: number; policyIds?: number[]; stepCount: number}[];
    pagination: Pagination }>;
  register(request: {agent: Agent; conflictMode?: "strict" | "overwrite"; evaluators?: EvaluatorSchema[]; forceReplace?: boolean;
    steps?: StepSchema[]; targetType?: string | null; targetId?: string | null}, options?: RequestOptions): Promise<{
      rules?: Rule[]; created: boolean; overwriteApplied: boolean; overwriteChanges?: AgentOverwriteChanges | null;
      controlContract?: "rule-controls-v1" | null; agentGeneration?: string | null;
      controlResolutions?: ControlReceipt[] | null }>;
  get(request: {agentName: string}, options?: RequestOptions): Promise<{agent: Agent; evaluators?: EvaluatorSchema[]; steps?: StepSchema[]}>;
  update(request: {agentName: string; body: {removeEvaluators?: string[]; removeSteps?: StepKey[]}}, options?: RequestOptions): Promise<{evaluatorsRemoved?: string[]; stepsRemoved?: StepKey[]}>;
  listRules(request: {agentName: string}, options?: RequestOptions): Promise<{rules: Rule[];
    controlContract?: "rule-controls-v1" | null; agentGeneration?: string | null;
    controlResolutions?: ControlReceipt[] | null}>;
  listEvaluators(request: {agentName: string}, options?: RequestOptions): Promise<{evaluators: EvaluatorSchema[]}>;
  getEvaluator(request: {agentName: string; evaluatorName: string}, options?: RequestOptions): Promise<EvaluatorSchema>;
}
export interface RuleBinding { ruleId: number; id: number; enabled: boolean; targetType: string; targetId: string; createdAt: Date; updatedAt: Date }
export interface RuleBindingsApi {
  list(request?: {cursor?: string | null; limit?: number; targetType?: string | null; targetId?: string | null; ruleId?: number | null}, options?: RequestOptions): Promise<{bindings?: RuleBinding[]; pagination: Pagination}>;
  get(request: {bindingId: number}, options?: RequestOptions): Promise<RuleBinding>;
}
export interface EvaluatorsApi { list(options?: RequestOptions): Promise<Record<string, {
  name: string; description: string; configSchema: JsonObject; requiresApiKey: boolean; timeoutMs: number; version: string
}>> }
export interface RuleCheckEvent {
  action: RuleDecision; agentName: string; appliesTo: string; checkStage: "pre" | "post"; confidence: number;
  checkId?: string; ruleId: number; ruleName: string; errorMessage?: string | null; evaluatorName?: string | null;
  executionDurationMs?: number | null; matched: boolean; metadata?: JsonObject; selectorPath?: string | null;
  spanId: string; timestamp?: Date; traceId: string;
}
export interface ObservabilityApi { ingestEvents(request: {events: RuleCheckEvent[]}, options?: RequestOptions): Promise<{
  received: number; enqueued: number; dropped: number; stored?: number | null; duplicates?: number | null; status: string
}> }
