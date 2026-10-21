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
import { Rule, Rule$inboundSchema } from "./rule.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  RegisterAgentOverwriteChanges,
  RegisterAgentOverwriteChanges$inboundSchema,
} from "./register-agent-overwrite-changes.js";

/**
 * Response from agent initialization.
 */
export type RegisterAgentResponse = {
  /**
   * Active protection rules for the agent
   */
  rules?: Array<Rule> | undefined;
  /**
   * True if agent was newly created, False if updated
   */
  created: boolean;
  /**
   * True if overwrite mode changed registration data on an existing agent
   */
  overwriteApplied: boolean;
  /**
   * Detailed change summary for register overwrite mode.
   */
  overwriteChanges?: RegisterAgentOverwriteChanges | undefined;
};

/** @internal */
export const RegisterAgentResponse$inboundSchema: z.ZodMiniType<
  RegisterAgentResponse,
  unknown
> = z.pipe(
  z.object({
    rules: types.optional(z.array(Rule$inboundSchema)),
    created: types.boolean(),
    overwrite_applied: z._default(types.boolean(), false),
    overwrite_changes: types.optional(RegisterAgentOverwriteChanges$inboundSchema),
  }),
  z.transform((v) => {
    return remap$(v, {
      "overwrite_applied": "overwriteApplied",
      "overwrite_changes": "overwriteChanges",
    });
  }),
);

export function registerResponseFromJSON(
  jsonString: string,
): SafeParseResult<RegisterAgentResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => RegisterAgentResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'RegisterAgentResponse' from JSON`,
  );
}
