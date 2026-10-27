// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import {
  RuleDefinitionOutput,
  RuleDefinitionOutput$inboundSchema,
} from "./rule-definition-output.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
export type RuleRule = RuleDefinitionOutput;

/**
 * A rule with identity and configuration.
 *
 * @remarks
 *
 * ``rule`` is the hosted Rule definition.
 */
export type Rule = {
  rule: RuleDefinitionOutput;
  id: number;
  name: string;
};

/** @internal */
export const RuleRule$inboundSchema: z.ZodMiniType<
  RuleRule,
  unknown
> = RuleDefinitionOutput$inboundSchema;

export function ruleRuleFromJSON(
  jsonString: string,
): SafeParseResult<RuleRule, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleRule$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleRule' from JSON`,
  );
}

/** @internal */
export const Rule$inboundSchema: z.ZodMiniType<Rule, unknown> = z.object({
  rule: RuleDefinitionOutput$inboundSchema,
  id: types.number(),
  name: types.string(),
});

export function ruleFromJSON(
  jsonString: string,
): SafeParseResult<Rule, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => Rule$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'Rule' from JSON`,
  );
}
