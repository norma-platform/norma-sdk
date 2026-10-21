// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import * as openEnums from "../types/enums.js";
import { OpenEnum } from "../types/enums.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import {
  ActionDecision,
  ActionDecision$inboundSchema,
  ActionDecision$outboundSchema,
} from "./action-decision.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

/**
 * Check stage: 'pre' or 'post'
 */
export const CheckStage = {
  Pre: "pre",
  Post: "post",
} as const;
/**
 * Check stage: 'pre' or 'post'
 */
export type CheckStage = OpenEnum<typeof CheckStage>;

/**
 * Represents one observed rule result, not a distinct request or proof an action ran.
 *
 * @remarks
 *
 * This is the core observability data model, capturing:
 * - Identity: check_id; correlation: trace_id and span_id
 * - Context: agent, rule, check stage, applies to
 * - Result: recorded rule response, whether matched, evaluator confidence score
 * - Timing: when it happened, how long it took
 * - Optional details: evaluator name, selector path, errors, metadata
 *
 * Attributes:
 *     check_id: Unique ID for this specific rule execution
 *     trace_id: Producer-reported trace correlation ID; may repeat across checks
 *     span_id: Producer-reported span correlation ID; may repeat across checks
 *     agent_name: Identifier of the agent that executed the rule
 *     rule_id: Database ID of the rule
 *     rule_name: Name of the rule (denormalized for queries)
 *     check_stage: "pre" (before execution) or "post" (after execution)
 *     applies_to: "llm_call", "tool_call", or a custom step type with a "_call" suffix
 *     action: Recorded configured/effective rule response (deny, steer, observe)
 *     matched: Whether the rule evaluator matched
 *     confidence: Evaluator value (0.0-1.0), not calibrated safety confidence
 *     timestamp: When the rule was executed (UTC)
 *     execution_duration_ms: How long the rule check took
 *     evaluator_name: Name of the evaluator used
 *     selector_path: The selector path used to extract data
 *     error_message: Error message if check failed
 *     metadata: Additional metadata for extensibility
 */
export type RuleCheckEvent = {
  action: ActionDecision;
  /**
   * Identifier of the agent
   */
  agentName: string;
  /**
   * Type of call or custom step type with a '_call' suffix
   */
  appliesTo: string;
  /**
   * Check stage: 'pre' or 'post'
   */
  checkStage: CheckStage;
  /**
   * Evaluator aggregate (0.0 to 1.0), not calibrated safety confidence
   */
  confidence: number;
  /**
   * Unique ID for this rule execution
   */
  checkId?: string | undefined;
  /**
   * Database ID of the rule
   */
  ruleId: number;
  /**
   * Name of the rule (denormalized)
   */
  ruleName: string;
  /**
   * Error message if check failed
   */
  errorMessage?: string | null | undefined;
  /**
   * Name of the evaluator used
   */
  evaluatorName?: string | null | undefined;
  /**
   * Execution duration in milliseconds
   */
  executionDurationMs?: number | null | undefined;
  /**
   * Whether the evaluator matched (True) or not (False)
   */
  matched: boolean;
  /**
   * Additional metadata
   */
  metadata?: { [k: string]: any } | undefined;
  /**
   * Selector path used to extract data
   */
  selectorPath?: string | null | undefined;
  /**
   * Producer-reported span correlation ID; may repeat across checks
   */
  spanId: string;
  /**
   * When the rule was executed (UTC)
   */
  timestamp?: Date | undefined;
  /**
   * Producer-reported trace correlation ID; may repeat across checks
   */
  traceId: string;
};

/** @internal */
export const CheckStage$inboundSchema: z.ZodMiniType<CheckStage, unknown> =
  openEnums.inboundSchema(CheckStage);
/** @internal */
export const CheckStage$outboundSchema: z.ZodMiniType<string, CheckStage> =
  openEnums.outboundSchema(CheckStage);

/** @internal */
export const RuleCheckEvent$inboundSchema: z.ZodMiniType<
  RuleCheckEvent,
  unknown
> = z.pipe(
  z.object({
    action: ActionDecision$inboundSchema,
    agent_name: types.string(),
    applies_to: types.string(),
    check_stage: CheckStage$inboundSchema,
    confidence: types.number(),
    check_id: types.optional(types.string()),
    rule_id: types.number(),
    rule_name: types.string(),
    error_message: z.optional(z.nullable(types.string())),
    evaluator_name: z.optional(z.nullable(types.string())),
    execution_duration_ms: z.optional(z.nullable(types.number())),
    matched: types.boolean(),
    metadata: types.optional(z.record(z.string(), z.any())),
    selector_path: z.optional(z.nullable(types.string())),
    span_id: types.string(),
    timestamp: types.optional(types.date()),
    trace_id: types.string(),
  }),
  z.transform((v) => {
    return remap$(v, {
      "agent_name": "agentName",
      "applies_to": "appliesTo",
      "check_stage": "checkStage",
      "check_id": "checkId",
      "rule_id": "ruleId",
      "rule_name": "ruleName",
      "error_message": "errorMessage",
      "evaluator_name": "evaluatorName",
      "execution_duration_ms": "executionDurationMs",
      "selector_path": "selectorPath",
      "span_id": "spanId",
      "trace_id": "traceId",
    });
  }),
);
/** @internal */
export type RuleCheckEvent$Outbound = {
  action: string;
  agent_name: string;
  applies_to: string;
  check_stage: string;
  confidence: number;
  check_id?: string | undefined;
  rule_id: number;
  rule_name: string;
  error_message?: string | null | undefined;
  evaluator_name?: string | null | undefined;
  execution_duration_ms?: number | null | undefined;
  matched: boolean;
  metadata?: { [k: string]: any } | undefined;
  selector_path?: string | null | undefined;
  span_id: string;
  timestamp?: string | undefined;
  trace_id: string;
};

/** @internal */
export const RuleCheckEvent$outboundSchema: z.ZodMiniType<
  RuleCheckEvent$Outbound,
  RuleCheckEvent
> = z.pipe(
  z.object({
    action: ActionDecision$outboundSchema,
    agentName: z.string(),
    appliesTo: z.string(),
    checkStage: CheckStage$outboundSchema,
    confidence: z.number(),
    checkId: z.optional(z.string()),
    ruleId: z.int(),
    ruleName: z.string(),
    errorMessage: z.optional(z.nullable(z.string())),
    evaluatorName: z.optional(z.nullable(z.string())),
    executionDurationMs: z.optional(z.nullable(z.number())),
    matched: z.boolean(),
    metadata: z.optional(z.record(z.string(), z.any())),
    selectorPath: z.optional(z.nullable(z.string())),
    spanId: z.string(),
    timestamp: z.optional(z.pipe(z.date(), z.transform(v => v.toISOString()))),
    traceId: z.string(),
  }),
  z.transform((v) => {
    return remap$(v, {
      agentName: "agent_name",
      appliesTo: "applies_to",
      checkStage: "check_stage",
      checkId: "check_id",
      ruleId: "rule_id",
      ruleName: "rule_name",
      errorMessage: "error_message",
      evaluatorName: "evaluator_name",
      executionDurationMs: "execution_duration_ms",
      selectorPath: "selector_path",
      spanId: "span_id",
      traceId: "trace_id",
    });
  }),
);

export function ruleExecutionEventToJSON(
  ruleExecutionEvent: RuleCheckEvent,
): string {
  return JSON.stringify(
    RuleCheckEvent$outboundSchema.parse(ruleExecutionEvent),
  );
}
export function ruleExecutionEventFromJSON(
  jsonString: string,
): SafeParseResult<RuleCheckEvent, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleCheckEvent$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleCheckEvent' from JSON`,
  );
}
