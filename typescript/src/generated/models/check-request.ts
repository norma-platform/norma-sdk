// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { ClosedEnum } from "../types/enums.js";
import { Step, Step$Outbound, Step$outboundSchema } from "./step.js";

/**
 * Check stage: 'pre' or 'post'
 */
export const Stage = {
  Pre: "pre",
  Post: "post",
} as const;
/**
 * Check stage: 'pre' or 'post'
 */
export type Stage = ClosedEnum<typeof Stage>;

/**
 * Request model for check analysis.
 *
 * @remarks
 *
 * Requests a rule-governed checkpoint decision for an agent interaction.
 *
 * Attributes:
 *     agent_name: Unique identifier of the agent making the request
 *     step: Step payload for check
 *     stage: 'pre' (before execution) or 'post' (after execution)
 *     target_type: Optional opaque target kind. When set together with
 *         ``target_id``, the server merges rules bound to that target
 *         into the effective set, in addition to the agent's direct and
 *         policy-derived rules.
 *     target_id: Optional opaque target identifier. Required when
 *         ``target_type`` is set.
 */
export type CheckRequest = {
  executionMode?: "hosted";
  /**
   * Identifier of the agent making the check request
   */
  agentName: string;
  /**
   * Check stage: 'pre' or 'post'
   */
  stage: Stage;
  /**
   * Runtime payload for an agent step invocation.
   */
  step: Step;
  /**
   * Optional opaque target identifier. Required when target_type is set.
   */
  targetId?: string | null | undefined;
  /**
   * Optional opaque target kind. When set together with target_id, the server merges rules bound to that target into the effective set, in addition to the agent's direct and policy-derived rules.
   */
  targetType?: string | null | undefined;
};

/** @internal */
export const Stage$outboundSchema: z.ZodMiniEnum<typeof Stage> = z.enum(Stage);

/** @internal */
export type CheckRequest$Outbound = {
  execution_mode?: "hosted";
  agent_name: string;
  stage: string;
  step: Step$Outbound;
  target_id?: string | null | undefined;
  target_type?: string | null | undefined;
};

/** @internal */
export const CheckRequest$outboundSchema: z.ZodMiniType<
  CheckRequest$Outbound,
  CheckRequest
> = z.pipe(
  z.object({
    executionMode: z.optional(z.literal("hosted")),
    agentName: z.string(),
    stage: Stage$outboundSchema,
    step: Step$outboundSchema,
    targetId: z.optional(z.nullable(z.string())),
    targetType: z.optional(z.nullable(z.string())),
  }),
  z.transform((v) => {
    return remap$(v, {
      executionMode: "execution_mode",
      agentName: "agent_name",
      targetId: "target_id",
      targetType: "target_type",
    });
  }),
);

export function checkRequestToJSON(
  checkRequest: CheckRequest,
): string {
  return JSON.stringify(
    CheckRequest$outboundSchema.parse(checkRequest),
  );
}
