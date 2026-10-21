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
import { SDKValidationError } from "./errors/sdk-validation-error.js";

export const Stages = {
  Pre: "pre",
  Post: "post",
} as const;
export type Stages = OpenEnum<typeof Stages>;

/**
 * Defines when a rule applies to a Step.
 */
export type RuleScope = {
  /**
   * Check stages this rule applies to
   */
  stages?: Array<Stages> | null | undefined;
  /**
   * RE2 pattern matched with search() against step name
   */
  stepNameRegex?: string | null | undefined;
  /**
   * Exact step names this rule applies to
   */
  stepNames?: Array<string> | null | undefined;
  /**
   * Step types this rule applies to (omit to apply to all types). Built-in types are 'tool' and 'llm'.
   */
  stepTypes?: Array<string> | null | undefined;
};

/** @internal */
export const Stages$inboundSchema: z.ZodMiniType<Stages, unknown> = openEnums
  .inboundSchema(Stages);
/** @internal */
export const Stages$outboundSchema: z.ZodMiniType<string, Stages> = openEnums
  .outboundSchema(Stages);

/** @internal */
export const RuleScope$inboundSchema: z.ZodMiniType<RuleScope, unknown> =
  z.pipe(
    z.object({
      stages: z.optional(z.nullable(z.array(Stages$inboundSchema))),
      step_name_regex: z.optional(z.nullable(types.string())),
      step_names: z.optional(z.nullable(z.array(types.string()))),
      step_types: z.optional(z.nullable(z.array(types.string()))),
    }),
    z.transform((v) => {
      return remap$(v, {
        "step_name_regex": "stepNameRegex",
        "step_names": "stepNames",
        "step_types": "stepTypes",
      });
    }),
  );
/** @internal */
export type RuleScope$Outbound = {
  stages?: Array<string> | null | undefined;
  step_name_regex?: string | null | undefined;
  step_names?: Array<string> | null | undefined;
  step_types?: Array<string> | null | undefined;
};

/** @internal */
export const RuleScope$outboundSchema: z.ZodMiniType<
  RuleScope$Outbound,
  RuleScope
> = z.pipe(
  z.object({
    stages: z.optional(z.nullable(z.array(Stages$outboundSchema))),
    stepNameRegex: z.optional(z.nullable(z.string())),
    stepNames: z.optional(z.nullable(z.array(z.string()))),
    stepTypes: z.optional(z.nullable(z.array(z.string()))),
  }),
  z.transform((v) => {
    return remap$(v, {
      stepNameRegex: "step_name_regex",
      stepNames: "step_names",
      stepTypes: "step_types",
    });
  }),
);

export function ruleScopeToJSON(ruleScope: RuleScope): string {
  return JSON.stringify(RuleScope$outboundSchema.parse(ruleScope));
}
export function ruleScopeFromJSON(
  jsonString: string,
): SafeParseResult<RuleScope, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleScope$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleScope' from JSON`,
  );
}
