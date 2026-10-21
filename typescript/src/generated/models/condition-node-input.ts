// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import {
  RuleSelector,
  RuleSelector$Outbound,
  RuleSelector$outboundSchema,
} from "./rule-selector.js";
import {
  EvaluatorSpec,
  EvaluatorSpec$Outbound,
  EvaluatorSpec$outboundSchema,
} from "./evaluator-spec.js";

/**
 * Recursive boolean condition tree for rule check.
 */
export type ConditionNodeInput = {
  /**
   * Logical AND over child conditions.
   */
  and?: Array<ConditionNodeInput> | null | undefined;
  /**
   * Leaf evaluator. Must be provided together with selector.
   */
  evaluator?: EvaluatorSpec | null | undefined;
  /**
   * Logical NOT over a single child condition.
   */
  not?: ConditionNodeInput | null | undefined;
  /**
   * Logical OR over child conditions.
   */
  or?: Array<ConditionNodeInput> | null | undefined;
  /**
   * Leaf selector. Must be provided together with evaluator.
   */
  selector?: RuleSelector | null | undefined;
};

/** @internal */
export type ConditionNodeInput$Outbound = {
  and?: Array<ConditionNodeInput$Outbound> | null | undefined;
  evaluator?: EvaluatorSpec$Outbound | null | undefined;
  not?: ConditionNodeInput$Outbound | null | undefined;
  or?: Array<ConditionNodeInput$Outbound> | null | undefined;
  selector?: RuleSelector$Outbound | null | undefined;
};

/** @internal */
export const ConditionNodeInput$outboundSchema: z.ZodMiniType<
  ConditionNodeInput$Outbound,
  ConditionNodeInput
> = z.object({
  and: z.optional(
    z.nullable(z.array(z.lazy(() => ConditionNodeInput$outboundSchema))),
  ),
  evaluator: z.optional(z.nullable(EvaluatorSpec$outboundSchema)),
  not: z.optional(z.nullable(z.lazy(() => ConditionNodeInput$outboundSchema))),
  or: z.optional(
    z.nullable(z.array(z.lazy(() => ConditionNodeInput$outboundSchema))),
  ),
  selector: z.optional(z.nullable(RuleSelector$outboundSchema)),
});

export function conditionNodeInputToJSON(
  conditionNodeInput: ConditionNodeInput,
): string {
  return JSON.stringify(
    ConditionNodeInput$outboundSchema.parse(conditionNodeInput),
  );
}
