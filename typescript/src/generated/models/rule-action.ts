// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import {
  ActionDecision,
  ActionDecision$inboundSchema,
  ActionDecision$outboundSchema,
} from "./action-decision.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  SteeringContext,
  SteeringContext$inboundSchema,
  SteeringContext$Outbound,
  SteeringContext$outboundSchema,
} from "./steering-context.js";

/**
 * What to do when rule matches.
 */
export type RuleAction = {
  decision: ActionDecision;
  /**
   * Steering context object for steer actions. Strongly recommended when decision='steer' to provide correction suggestions. If not provided, the evaluator result message will be used as fallback.
   */
  steeringContext?: SteeringContext | null | undefined;
};

/** @internal */
export const RuleAction$inboundSchema: z.ZodMiniType<
  RuleAction,
  unknown
> = z.pipe(
  z.object({
    decision: ActionDecision$inboundSchema,
    steering_context: z.optional(z.nullable(SteeringContext$inboundSchema)),
  }),
  z.transform((v) => {
    return remap$(v, {
      "steering_context": "steeringContext",
    });
  }),
);
/** @internal */
export type RuleAction$Outbound = {
  decision: string;
  steering_context?: SteeringContext$Outbound | null | undefined;
};

/** @internal */
export const RuleAction$outboundSchema: z.ZodMiniType<
  RuleAction$Outbound,
  RuleAction
> = z.pipe(
  z.object({
    decision: ActionDecision$outboundSchema,
    steeringContext: z.optional(z.nullable(SteeringContext$outboundSchema)),
  }),
  z.transform((v) => {
    return remap$(v, {
      steeringContext: "steering_context",
    });
  }),
);

export function ruleActionToJSON(ruleAction: RuleAction): string {
  return JSON.stringify(RuleAction$outboundSchema.parse(ruleAction));
}
export function ruleActionFromJSON(
  jsonString: string,
): SafeParseResult<RuleAction, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RuleAction$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RuleAction' from JSON`,
  );
}
