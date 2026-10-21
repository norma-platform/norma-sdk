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

export type AgentRulesResponse = {
  /**
   * List of agent-associated rules matching the requested state filters (all associated rules by default, including disabled and unrendered rules)
   */
  rules: Array<Rule>;
};

/** @internal */
export const AgentRulesResponse$inboundSchema: z.ZodMiniType<
  AgentRulesResponse,
  unknown
> = z.object({
  rules: z.array(Rule$inboundSchema),
});

export function agentRulesResponseFromJSON(
  jsonString: string,
): SafeParseResult<AgentRulesResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => AgentRulesResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'AgentRulesResponse' from JSON`,
  );
}
