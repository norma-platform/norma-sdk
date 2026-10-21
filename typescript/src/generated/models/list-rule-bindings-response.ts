// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";
import {
  GetRuleBindingResponse,
  GetRuleBindingResponse$inboundSchema,
} from "./get-rule-binding-response.js";
import {
  PaginationInfo,
  PaginationInfo$inboundSchema,
} from "./pagination-info.js";

/**
 * Paginated/filtered list of rule bindings.
 */
export type ListRuleBindingsResponse = {
  bindings?: Array<GetRuleBindingResponse> | undefined;
  /**
   * Pagination metadata for cursor-based pagination.
   */
  pagination: PaginationInfo;
};

/** @internal */
export const ListRuleBindingsResponse$inboundSchema: z.ZodMiniType<
  ListRuleBindingsResponse,
  unknown
> = z.object({
  bindings: types.optional(z.array(GetRuleBindingResponse$inboundSchema)),
  pagination: PaginationInfo$inboundSchema,
});

export function listRuleBindingsResponseFromJSON(
  jsonString: string,
): SafeParseResult<ListRuleBindingsResponse, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => ListRuleBindingsResponse$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'ListRuleBindingsResponse' from JSON`,
  );
}
