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

/**
 * Evaluator specification. See GET /evaluators for available evaluators and schemas.
 *
 * @remarks
 *
 * Names and configuration are opaque wire DTOs returned by the hosted service.
 * This SDK neither installs evaluators nor accepts provider credentials for them.
 */
export type EvaluatorSpec = {
  /**
   * Evaluator-specific configuration
   */
  config: { [k: string]: any };
  /**
   * Evaluator name or agent-scoped reference (agent:evaluator)
   */
  name: string;
};

/** @internal */
export const EvaluatorSpec$inboundSchema: z.ZodMiniType<
  EvaluatorSpec,
  unknown
> = z.object({
  config: z.record(z.string(), z.any()),
  name: types.string(),
});
/** @internal */
export type EvaluatorSpec$Outbound = {
  config: { [k: string]: any };
  name: string;
};

/** @internal */
export const EvaluatorSpec$outboundSchema: z.ZodMiniType<
  EvaluatorSpec$Outbound,
  EvaluatorSpec
> = z.object({
  config: z.record(z.string(), z.any()),
  name: z.string(),
});

export function evaluatorSpecToJSON(evaluatorSpec: EvaluatorSpec): string {
  return JSON.stringify(EvaluatorSpec$outboundSchema.parse(evaluatorSpec));
}
export function evaluatorSpecFromJSON(
  jsonString: string,
): SafeParseResult<EvaluatorSpec, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => EvaluatorSpec$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'EvaluatorSpec' from JSON`,
  );
}
