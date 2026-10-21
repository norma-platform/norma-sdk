// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { safeParse } from "../lib/schemas.js";
import { Result as SafeParseResult } from "../types/fp.js";
import * as types from "../types/primitives.js";
import { smartUnion } from "../types/smart-union.js";
import { SDKValidationError } from "./errors/sdk-validation-error.js";

export type Loc = string | number;

export type ValidationError = {
  loc: Array<string | number>;
  msg: string;
  type: string;
};

/** @internal */
export const Loc$inboundSchema: z.ZodMiniType<Loc, unknown> = smartUnion([
  types.string(),
  types.number(),
]);

export function locFromJSON(
  jsonString: string,
): SafeParseResult<Loc, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => Loc$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'Loc' from JSON`,
  );
}

/** @internal */
export const ValidationError$inboundSchema: z.ZodMiniType<
  ValidationError,
  unknown
> = z.object({
  loc: z.array(smartUnion([types.string(), types.number()])),
  msg: types.string(),
  type: types.string(),
});

export function validationErrorFromJSON(
  jsonString: string,
): SafeParseResult<ValidationError, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => ValidationError$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'ValidationError' from JSON`,
  );
}
