// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../../lib/primitives.js";

export type ListRuleBindingsApiV1RuleBindingsGetRequest = {
  /**
   * Opaque cursor returned as ``next_cursor`` on the previous page. Pass it back unchanged to fetch the next page.
   */
  cursor?: string | null | undefined;
  /**
   * Maximum bindings to return (default 20, max 100).
   */
  limit?: number | undefined;
  targetType?: string | null | undefined;
  targetId?: string | null | undefined;
  ruleId?: number | null | undefined;
};

/** @internal */
export type ListRuleBindingsApiV1RuleBindingsGetRequest$Outbound = {
  cursor?: string | null | undefined;
  limit: number;
  target_type?: string | null | undefined;
  target_id?: string | null | undefined;
  rule_id?: number | null | undefined;
};

/** @internal */
export const ListRuleBindingsApiV1RuleBindingsGetRequest$outboundSchema:
  z.ZodMiniType<
    ListRuleBindingsApiV1RuleBindingsGetRequest$Outbound,
    ListRuleBindingsApiV1RuleBindingsGetRequest
  > = z.pipe(
    z.object({
      cursor: z.optional(z.nullable(z.string())),
      limit: z._default(z.int(), 20),
      targetType: z.optional(z.nullable(z.string())),
      targetId: z.optional(z.nullable(z.string())),
      ruleId: z.optional(z.nullable(z.int())),
    }),
    z.transform((v) => {
      return remap$(v, {
        targetType: "target_type",
        targetId: "target_id",
        ruleId: "rule_id",
      });
    }),
  );

export function listRuleBindingsApiV1RuleBindingsGetRequestToJSON(
  listRuleBindingsApiV1RuleBindingsGetRequest:
    ListRuleBindingsApiV1RuleBindingsGetRequest,
): string {
  return JSON.stringify(
    ListRuleBindingsApiV1RuleBindingsGetRequest$outboundSchema.parse(
      listRuleBindingsApiV1RuleBindingsGetRequest,
    ),
  );
}
