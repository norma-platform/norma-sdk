// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import {
  RuleSelector,
  RuleSelector$inboundSchema,
} from "./rule-selector.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  EvaluatorSpec,
  EvaluatorSpec$inboundSchema,
} from "./evaluator-spec.js";

/**
 * Recursive boolean condition tree for rule check.
 */
export type ConditionNodeOutput = {
  /**
   * Logical AND over child conditions.
   */
  and?: Array<ConditionNodeOutput> | null | undefined;
  /**
   * Leaf evaluator. Must be provided together with selector.
   */
  evaluator?: EvaluatorSpec | null | undefined;
  /**
   * Logical NOT over a single child condition.
   */
  not?: ConditionNodeOutput | null | undefined;
  /**
   * Logical OR over child conditions.
   */
  or?: Array<ConditionNodeOutput> | null | undefined;
  /**
   * Leaf selector. Must be provided together with evaluator.
   */
  selector?: RuleSelector | null | undefined;
};

/** @internal */
export const ConditionNodeOutput$inboundSchema: z.ZodMiniType<
  ConditionNodeOutput,
  unknown
> = z.object({
  and: z.optional(
    z.nullable(z.array(z.lazy(() => ConditionNodeOutput$inboundSchema))),
  ),
  evaluator: z.optional(z.nullable(EvaluatorSpec$inboundSchema)),
  not: z.optional(z.nullable(z.lazy(() => ConditionNodeOutput$inboundSchema))),
  or: z.optional(
    z.nullable(z.array(z.lazy(() => ConditionNodeOutput$inboundSchema))),
  ),
  selector: z.optional(z.nullable(RuleSelector$inboundSchema)),
});

export function conditionNodeOutputFromJSON(
  jsonString: string,
): SafeParseResult<ConditionNodeOutput, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => ConditionNodeOutput$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'ConditionNodeOutput' from JSON`,
  );
}
