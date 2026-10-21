// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../../lib/primitives.js";

export type GetRuleBindingApiV1RuleBindingsBindingIdGetRequest = {
  bindingId: number;
};

/** @internal */
export type GetRuleBindingApiV1RuleBindingsBindingIdGetRequest$Outbound =
  {
    binding_id: number;
  };

/** @internal */
export const GetRuleBindingApiV1RuleBindingsBindingIdGetRequest$outboundSchema:
  z.ZodMiniType<
    GetRuleBindingApiV1RuleBindingsBindingIdGetRequest$Outbound,
    GetRuleBindingApiV1RuleBindingsBindingIdGetRequest
  > = z.pipe(
    z.object({
      bindingId: z.int(),
    }),
    z.transform((v) => {
      return remap$(v, {
        bindingId: "binding_id",
      });
    }),
  );

export function getRuleBindingApiV1RuleBindingsBindingIdGetRequestToJSON(
  getRuleBindingApiV1RuleBindingsBindingIdGetRequest:
    GetRuleBindingApiV1RuleBindingsBindingIdGetRequest,
): string {
  return JSON.stringify(
    GetRuleBindingApiV1RuleBindingsBindingIdGetRequest$outboundSchema
      .parse(getRuleBindingApiV1RuleBindingsBindingIdGetRequest),
  );
}
