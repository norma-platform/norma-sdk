// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { CheckOutcomeSchema, SemanticObservationSchema, type CheckOutcome, type SemanticObservation } from "../../internal/semantic-check.js";
import { remap as remap$ } from "../lib/primitives.js";
import { ActionDecision$inboundSchema, type ActionDecision } from "./action-decision.js";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

/**
 * Result from a rule evaluator.
 *
 * @remarks
 *
 * The `error` field indicates evaluator failures, NOT validation failures:
 * - Set `error` for: evaluator crashes, timeouts, missing dependencies, external service errors
 * - Do NOT set `error` for: invalid input, syntax errors, schema violations, constraint failures
 *
 * An evaluator error is not a non-match or permission to proceed. Hosted checkpoints fail closed on evaluator errors.
 * When `error` is None, `matched` reflects the actual validation result.
 *
 * This distinction allows:
 * - Clients to distinguish "data violated rules" from "evaluator is broken"
 * - Observability systems to monitor evaluator health separately from validation outcomes
 */
export type EvaluatorResult = {
  /**
   * Evaluator value; not calibrated correctness confidence or permission
   */
  confidence: number;
  outcome?: CheckOutcome | null | undefined;
  semanticObservation?: SemanticObservation | null | undefined;
  failedConditionIds?: Array<string> | undefined;
  unresolvedEvidenceIds?: Array<string> | undefined;
  unresolvedAction?: ActionDecision | null | undefined;
  /**
   * Error message if check failed internally. When set, matched=False is due to error, not actual check.
   */
  error?: string | null | undefined;
  /**
   * Whether the pattern matched
   */
  matched: boolean;
  /**
   * Explanation of the result
   */
  message?: string | null | undefined;
  /**
   * Additional result metadata
   */
  metadata?: { [k: string]: any } | null | undefined;
};

/** @internal */
export const EvaluatorResult$inboundSchema: z.ZodMiniType<
  EvaluatorResult,
  unknown
> = z.pipe(z.object({
  confidence: types.number(),
  outcome: z.optional(z.nullable(CheckOutcomeSchema)),
  semantic_observation: z.optional(z.nullable(SemanticObservationSchema)),
  failed_condition_ids: z.optional(z.array(z.string())),
  unresolved_evidence_ids: z.optional(z.array(z.string())),
  unresolved_action: z.optional(z.nullable(ActionDecision$inboundSchema)),
  error: z.optional(z.nullable(types.string())),
  matched: types.boolean(),
  message: z.optional(z.nullable(types.string())),
  metadata: z.optional(z.nullable(z.record(z.string(), z.any()))),
}), z.transform((v) => remap$(v, {
  "semantic_observation": "semanticObservation",
  "failed_condition_ids": "failedConditionIds",
  "unresolved_evidence_ids": "unresolvedEvidenceIds",
  "unresolved_action": "unresolvedAction",
})));

export function evaluatorResultFromJSON(
  jsonString: string,
): SafeParseResult<EvaluatorResult, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => EvaluatorResult$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'EvaluatorResult' from JSON`,
  );
}
