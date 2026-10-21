// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

/**
 * Selects data from a Step payload.
 *
 * @remarks
 *
 * - path: which slice of the Step to feed into the evaluator. Optional, defaults to "*"
 *   meaning the entire Step object.
 */
export type RuleSelector = {
  /**
   * Path to data using dot notation. Examples: 'input', 'output', 'context.user_id', 'name', 'type', '*'
   */
  path?: string | null | undefined;
};

/** @internal */
export const RuleSelector$inboundSchema: z.ZodMiniType<
  RuleSelector,
  unknown
> = z.object({
  path: z.optional(z.nullable(types.string())),
});
/** @internal */
export type RuleSelector$Outbound = {
  path?: string | null | undefined;
};

/** @internal */
export const RuleSelector$outboundSchema: z.ZodMiniType<
  RuleSelector$Outbound,
  RuleSelector
> = z.object({
  path: z.optional(z.nullable(z.string())),
});

export function ruleSelectorToJSON(
  ruleSelector: RuleSelector,
): string {
  return JSON.stringify(RuleSelector$outboundSchema.parse(ruleSelector));
}
export function ruleSelectorFromJSON(
  jsonString: string,
): SafeParseResult<RuleSelector, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleSelector$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleSelector' from JSON`,
  );
}
