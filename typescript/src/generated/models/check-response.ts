// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { RecoveryFeedbackSchema, type RecoveryFeedback } from "../../internal/semantic-check.js";
import { remap as remap$ } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { RuleMatch, RuleMatch$inboundSchema } from "./rule-match.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import { ControlEvidenceSnapshot$inboundSchema, ControlReceipt$inboundSchema, type ControlEvidenceSnapshot, type ControlReceipt } from "./control-evidence.js";

/**
 * Response model from check analysis (server-side).
 *
 * @remarks
 *
 * This is what the server returns. The SDK may transform this
 * into an CheckResult for client convenience.
 *
 * Attributes:
 *     allowed: Rule-checkpoint permission aggregate, not proof that content is safe
 *     confidence: Aggregate of per-rule results; not calibrated safety confidence
 *     reason: Optional explanation for the decision
 *     matches: List of rules that matched/triggered (if any)
 *     errors: List of rules that failed during check (if any)
 *     non_matches: List of rules that were evaluated but did not match (if any)
 */
export type CheckResponse = {
  executionContract?: string;
  controlContract?: "rule-controls-v1" | null | undefined;
  agentGeneration?: string | null | undefined;
  controlResolutions?: Array<ControlReceipt> | null | undefined;
  controlEvidence?: Array<ControlEvidenceSnapshot> | null | undefined;
  /**
   * Evaluator aggregate (0.0 to 1.0), not calibrated safety confidence
   */
  confidence: number;
  unresolved?: Array<RuleMatch> | null | undefined;
  recoveryFeedback?: Array<RecoveryFeedback> | null | undefined;
  /**
   * List of rules that failed during check (if any)
   */
  errors?: Array<RuleMatch> | null | undefined;
  /**
   * Whether the runtime permits this checkpoint under its evaluated rules
   */
  allowed: boolean;
  /**
   * List of rules that matched/triggered (if any)
   */
  matches?: Array<RuleMatch> | null | undefined;
  /**
   * List of rules that were evaluated but did not match (if any)
   */
  nonMatches?: Array<RuleMatch> | null | undefined;
  /**
   * Explanation for the decision
   */
  reason?: string | null | undefined;
};

/** @internal */
export const CheckResponse$inboundSchema: z.ZodMiniType<
  CheckResponse,
  unknown
> = z.pipe(
  z.object({
    execution_contract: z.optional(z.string()),
    control_contract: z.optional(z.nullable(z.literal("rule-controls-v1"))),
    agent_generation: z.optional(z.nullable(z.string())),
    control_resolutions: z.optional(z.nullable(z.array(ControlReceipt$inboundSchema))),
    control_evidence: z.optional(z.nullable(z.array(ControlEvidenceSnapshot$inboundSchema))),
    confidence: types.number(),
    unresolved: z.optional(z.nullable(z.array(RuleMatch$inboundSchema))),
    recovery_feedback: z.optional(z.nullable(z.array(RecoveryFeedbackSchema))),
    errors: z.optional(z.nullable(z.array(RuleMatch$inboundSchema))),
    allowed: z.boolean(),
    matches: z.optional(z.nullable(z.array(RuleMatch$inboundSchema))),
    non_matches: z.optional(z.nullable(z.array(RuleMatch$inboundSchema))),
    reason: z.optional(z.nullable(types.string())),
  }),
  z.transform((v) => {
    const result = remap$(v, {
      "execution_contract": "executionContract",
      "control_contract": "controlContract",
      "agent_generation": "agentGeneration",
      "control_resolutions": "controlResolutions",
      "control_evidence": "controlEvidence",
      "allowed": "allowed",
      "recovery_feedback": "recoveryFeedback",
      "non_matches": "nonMatches",
    });
    if (result.controlResolutions) Object.freeze(result.controlResolutions);
    if (result.controlEvidence) Object.freeze(result.controlEvidence);
    return result;
  }),
);

export function checkResponseFromJSON(
  jsonString: string,
): SafeParseResult<CheckResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => CheckResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'CheckResponse' from JSON`,
  );
}
