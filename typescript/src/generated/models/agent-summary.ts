// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

/**
 * Summary of an agent for list responses.
 */
export type AgentSummary = {
  /**
   * Number of active rules for this agent
   */
  activeRulesCount: number;
  /**
   * Unique identifier of the agent
   */
  agentName: string;
  /**
   * ISO 8601 timestamp when agent was created
   */
  createdAt?: string | null | undefined;
  /**
   * Number of evaluators registered with the agent
   */
  evaluatorCount: number;
  /**
   * IDs of policies associated with the agent
   */
  policyIds?: Array<number> | undefined;
  /**
   * Number of steps registered with the agent
   */
  stepCount: number;
};

/** @internal */
export const AgentSummary$inboundSchema: z.ZodMiniType<AgentSummary, unknown> =
  z.pipe(
    z.object({
      active_rules_count: z._default(types.number(), 0),
      agent_name: types.string(),
      created_at: z.optional(z.nullable(types.string())),
      evaluator_count: z._default(types.number(), 0),
      policy_ids: types.optional(z.array(types.number())),
      step_count: z._default(types.number(), 0),
    }),
    z.transform((v) => {
      return remap$(v, {
        "active_rules_count": "activeRulesCount",
        "agent_name": "agentName",
        "created_at": "createdAt",
        "evaluator_count": "evaluatorCount",
        "policy_ids": "policyIds",
        "step_count": "stepCount",
      });
    }),
  );

export function agentSummaryFromJSON(
  jsonString: string,
): SafeParseResult<AgentSummary, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => AgentSummary$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'AgentSummary' from JSON`,
  );
}
