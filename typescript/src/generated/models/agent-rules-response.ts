// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import { Rule, Rule$inboundSchema } from "./rule.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import { ControlReceipt, ControlReceipt$inboundSchema } from "./control-evidence.js";

export type AgentRulesResponse = {
  /**
   * List of agent-associated Rules matching the requested state filters (all associated Rules by default, including disabled Rules)
   */
  rules: Array<Rule>;
  controlContract?: "rule-controls-v1" | null;
  agentGeneration?: string | null;
  controlResolutions?: Array<ControlReceipt> | null;
};

/** @internal */
export const AgentRulesResponse$inboundSchema: z.ZodMiniType<
  AgentRulesResponse,
  unknown
> = z.pipe(z.object({
  rules: z.array(Rule$inboundSchema),
  control_contract: z.optional(z.nullable(z.literal("rule-controls-v1"))),
  agent_generation: z.optional(z.nullable(z.string())),
  control_resolutions: z.optional(z.nullable(z.array(ControlReceipt$inboundSchema))),
}), z.transform((v) => ({
  rules: v.rules,
  ...(v.control_contract === undefined ? {} : { controlContract: v.control_contract }),
  ...(v.agent_generation === undefined ? {} : { agentGeneration: v.agent_generation }),
  ...(v.control_resolutions === undefined ? {} : { controlResolutions: v.control_resolutions }),
})));

export function agentRulesResponseFromJSON(
  jsonString: string,
): SafeParseResult<AgentRulesResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => AgentRulesResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'AgentRulesResponse' from JSON`,
  );
}
