// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { smartUnion } from "../types/smart-union.js";
import {
  RuleDefinitionOutput,
  RuleDefinitionOutput$inboundSchema,
} from "./rule-definition-output.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  UnrenderedTemplateRule,
  UnrenderedTemplateRule$inboundSchema,
} from "./unrendered-template-rule.js";

export type RuleRule =
  | RuleDefinitionOutput
  | UnrenderedTemplateRule;

/**
 * A rule with identity and configuration.
 *
 * @remarks
 *
 * For rendered rules (raw or template-backed), ``rule`` is a
 * ``RuleDefinition``.  For unrendered template rules, ``rule``
 * is an ``UnrenderedTemplateRule``.
 */
export type Rule = {
  rule: RuleDefinitionOutput | UnrenderedTemplateRule;
  id: number;
  name: string;
};

/** @internal */
export const RuleRule$inboundSchema: z.ZodMiniType<
  RuleRule,
  unknown
> = smartUnion([
  RuleDefinitionOutput$inboundSchema,
  UnrenderedTemplateRule$inboundSchema,
]);

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
  rule: smartUnion([
    RuleDefinitionOutput$inboundSchema,
    UnrenderedTemplateRule$inboundSchema,
  ]),
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
