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
 * Detail view of a single rule binding.
 */
export type GetRuleBindingResponse = {
  ruleId: number;
  createdAt: Date;
  enabled: boolean;
  id: number;
  namespaceKey: string;
  targetId: string;
  targetType: string;
  updatedAt: Date;
};

/** @internal */
export const GetRuleBindingResponse$inboundSchema: z.ZodMiniType<
  GetRuleBindingResponse,
  unknown
> = z.pipe(
  z.object({
    rule_id: types.number(),
    created_at: types.date(),
    enabled: types.boolean(),
    id: types.number(),
    namespace_key: types.string(),
    target_id: types.string(),
    target_type: types.string(),
    updated_at: types.date(),
  }),
  z.transform((v) => {
    return remap$(v, {
      "rule_id": "ruleId",
      "created_at": "createdAt",
      "namespace_key": "namespaceKey",
      "target_id": "targetId",
      "target_type": "targetType",
      "updated_at": "updatedAt",
    });
  }),
);

export function getRuleBindingResponseFromJSON(
  jsonString: string,
): SafeParseResult<GetRuleBindingResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => GetRuleBindingResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'GetRuleBindingResponse' from JSON`,
  );
}
