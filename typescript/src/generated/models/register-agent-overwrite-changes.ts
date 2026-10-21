// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  RegisterAgentEvaluatorRemoval,
  RegisterAgentEvaluatorRemoval$inboundSchema,
} from "./register-agent-evaluator-removal.js";
import { StepKey, StepKey$inboundSchema } from "./step-key.js";

/**
 * Detailed change summary for register overwrite mode.
 */
export type RegisterAgentOverwriteChanges = {
  /**
   * Per-evaluator removal details, including active rule references
   */
  evaluatorRemovals?: Array<RegisterAgentEvaluatorRemoval> | undefined;
  /**
   * Evaluator names added by overwrite
   */
  evaluatorsAdded?: Array<string> | undefined;
  /**
   * Evaluator names removed by overwrite
   */
  evaluatorsRemoved?: Array<string> | undefined;
  /**
   * Existing evaluator names updated by overwrite
   */
  evaluatorsUpdated?: Array<string> | undefined;
  /**
   * Whether agent metadata changed
   */
  metadataChanged: boolean;
  /**
   * Steps added by overwrite
   */
  stepsAdded?: Array<StepKey> | undefined;
  /**
   * Steps removed by overwrite
   */
  stepsRemoved?: Array<StepKey> | undefined;
  /**
   * Existing steps updated by overwrite
   */
  stepsUpdated?: Array<StepKey> | undefined;
};

/** @internal */
export const RegisterAgentOverwriteChanges$inboundSchema: z.ZodMiniType<
  RegisterAgentOverwriteChanges,
  unknown
> = z.pipe(
  z.object({
    evaluator_removals: types.optional(
      z.array(RegisterAgentEvaluatorRemoval$inboundSchema),
    ),
    evaluators_added: types.optional(z.array(types.string())),
    evaluators_removed: types.optional(z.array(types.string())),
    evaluators_updated: types.optional(z.array(types.string())),
    metadata_changed: z._default(types.boolean(), false),
    steps_added: types.optional(z.array(StepKey$inboundSchema)),
    steps_removed: types.optional(z.array(StepKey$inboundSchema)),
    steps_updated: types.optional(z.array(StepKey$inboundSchema)),
  }),
  z.transform((v) => {
    return remap$(v, {
      "evaluator_removals": "evaluatorRemovals",
      "evaluators_added": "evaluatorsAdded",
      "evaluators_removed": "evaluatorsRemoved",
      "evaluators_updated": "evaluatorsUpdated",
      "metadata_changed": "metadataChanged",
      "steps_added": "stepsAdded",
      "steps_removed": "stepsRemoved",
      "steps_updated": "stepsUpdated",
    });
  }),
);

export function registerOverwriteChangesFromJSON(
  jsonString: string,
): SafeParseResult<RegisterAgentOverwriteChanges, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RegisterAgentOverwriteChanges$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RegisterAgentOverwriteChanges' from JSON`,
  );
}
