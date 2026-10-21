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

/**
 * Details for an evaluator removed during overwrite mode.
 */
export type RegisterAgentEvaluatorRemoval = {
  /**
   * IDs of active rules referencing this evaluator
   */
  ruleIds?: Array<number> | undefined;
  /**
   * Names of active rules referencing this evaluator
   */
  ruleNames?: Array<string> | undefined;
  /**
   * Evaluator name removed by overwrite
   */
  name: string;
  /**
   * Whether this evaluator is still referenced by active rules
   */
  referencedByActiveRules: boolean;
};

/** @internal */
export const RegisterAgentEvaluatorRemoval$inboundSchema: z.ZodMiniType<
  RegisterAgentEvaluatorRemoval,
  unknown
> = z.pipe(
  z.object({
    rule_ids: types.optional(z.array(types.number())),
    rule_names: types.optional(z.array(types.string())),
    name: types.string(),
    referenced_by_active_rules: z._default(types.boolean(), false),
  }),
  z.transform((v) => {
    return remap$(v, {
      "rule_ids": "ruleIds",
      "rule_names": "ruleNames",
      "referenced_by_active_rules": "referencedByActiveRules",
    });
  }),
);

export function registerEvaluatorRemovalFromJSON(
  jsonString: string,
): SafeParseResult<RegisterAgentEvaluatorRemoval, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RegisterAgentEvaluatorRemoval$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RegisterAgentEvaluatorRemoval' from JSON`,
  );
}
