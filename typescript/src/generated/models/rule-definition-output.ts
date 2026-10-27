// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import * as openEnums from "../types/enums.js";
import { OpenEnum } from "../types/enums.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import {
  ConditionNodeOutput,
  ConditionNodeOutput$inboundSchema,
} from "./condition-node-output.js";
import {
  RuleAction,
  RuleAction$inboundSchema,
} from "./rule-action.js";
import { RuleScope, RuleScope$inboundSchema } from "./rule-scope.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

/**
 * Where this rule executes
 */
export const Execution = {
  Server: "server",
  SDK: "sdk",
} as const;
/**
 * Where this rule executes
 */
export type Execution = OpenEnum<typeof Execution>;

/**
 * A rule definition to evaluate agent interactions.
 *
 * @remarks
 *
 * This model contains only the logic and configuration.
 * Identity fields (id, name) are managed by the database.
 */
export type RuleDefinitionOutput = {
  /**
   * What to do when rule matches.
   */
  action: RuleAction;
  /**
   * Recursive boolean condition tree for rule check.
   */
  condition: ConditionNodeOutput;
  /**
   * Detailed description of the rule
   */
  description?: string | null | undefined;
  mode?: "enforce" | "observe";
  enabled: boolean;
  /**
   * Where this rule executes
   */
  execution: Execution;
  /**
   * Defines when a rule applies to a Step.
   */
  scope?: RuleScope | undefined;
  /**
   * Tags for categorization
   */
  tags?: Array<string> | undefined;
};

/** @internal */
export const Execution$inboundSchema: z.ZodMiniType<Execution, unknown> =
  openEnums.inboundSchema(Execution);

/** @internal */
export const RuleDefinitionOutput$inboundSchema: z.ZodMiniType<
  RuleDefinitionOutput,
  unknown
> = z.pipe(
  z.object({
    action: RuleAction$inboundSchema,
    condition: ConditionNodeOutput$inboundSchema,
    description: z.optional(z.nullable(types.string())),
    mode: z.optional(z.enum(["enforce", "observe"])),
    enabled: z._default(types.boolean(), true),
    execution: Execution$inboundSchema,
    scope: types.optional(RuleScope$inboundSchema),
    tags: types.optional(z.array(types.string())),
  }),
  z.transform((value) => value as RuleDefinitionOutput),
);

export function ruleDefinitionOutputFromJSON(
  jsonString: string,
): SafeParseResult<RuleDefinitionOutput, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleDefinitionOutput$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleDefinitionOutput' from JSON`,
  );
}
