// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { ClosedEnum } from "../types/enums.js";
import {
  ConditionNodeInput,
  ConditionNodeInput$Outbound,
  ConditionNodeInput$outboundSchema,
} from "./condition-node-input.js";
import {
  RuleAction,
  RuleAction$Outbound,
  RuleAction$outboundSchema,
} from "./rule-action.js";
import {
  RuleScope,
  RuleScope$Outbound,
  RuleScope$outboundSchema,
} from "./rule-scope.js";

/**
 * Where this rule executes
 */
export const RuleDefinitionInputExecution = {
  Server: "server",
  SDK: "sdk",
} as const;
/**
 * Where this rule executes
 */
export type RuleDefinitionInputExecution = ClosedEnum<
  typeof RuleDefinitionInputExecution
>;

/**
 * A rule definition to evaluate agent interactions.
 *
 * @remarks
 *
 * This model contains only the logic and configuration.
 * Identity fields (id, name) are managed by the database.
 */
export type RuleDefinitionInput = {
  /**
   * What to do when rule matches.
   */
  action: RuleAction;
  /**
   * Recursive boolean condition tree for rule check.
   */
  condition: ConditionNodeInput;
  /**
   * Detailed description of the rule
   */
  description?: string | null | undefined;
  /**
   * Whether this rule is active
   */
  mode?: "enforce" | "observe";
  enabled?: boolean | undefined;
  /**
   * Where this rule executes
   */
  execution: RuleDefinitionInputExecution;
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
export const RuleDefinitionInputExecution$outboundSchema: z.ZodMiniEnum<
  typeof RuleDefinitionInputExecution
> = z.enum(RuleDefinitionInputExecution);

/** @internal */
export type RuleDefinitionInput$Outbound = {
  action: RuleAction$Outbound;
  condition: ConditionNodeInput$Outbound;
  description?: string | null | undefined;
  mode?: "enforce" | "observe";
  enabled: boolean;
  execution: string;
  scope?: RuleScope$Outbound | undefined;
  tags?: Array<string> | undefined;
};

/** @internal */
export const RuleDefinitionInput$outboundSchema: z.ZodMiniType<
  RuleDefinitionInput$Outbound,
  RuleDefinitionInput
> = z.pipe(
  z.object({
    action: RuleAction$outboundSchema,
    condition: ConditionNodeInput$outboundSchema,
    description: z.optional(z.nullable(z.string())),
    mode: z.optional(z.enum(["enforce", "observe"])),
    enabled: z._default(z.boolean(), true),
    execution: RuleDefinitionInputExecution$outboundSchema,
    scope: z.optional(RuleScope$outboundSchema),
    tags: z.optional(z.array(z.string())),
  }),
);

export function ruleDefinitionInputToJSON(
  ruleDefinitionInput: RuleDefinitionInput,
): string {
  return JSON.stringify(
    RuleDefinitionInput$outboundSchema.parse(ruleDefinitionInput),
  );
}
