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
import {
  TemplateDefinitionOutput,
  TemplateDefinitionOutput$inboundSchema,
} from "./template-definition-output.js";
import {
  TemplateValue,
  TemplateValue$inboundSchema,
} from "./template-value.js";

/**
 * Stored state of a template rule that hasn't been rendered yet.
 *
 * @remarks
 *
 * An unrendered template has a template definition and possibly partial
 * parameter values, but no concrete condition/action/execution fields.
 * It is always ``enabled=False`` and excluded from check.
 */
export type UnrenderedTemplateRule = {
  /**
   * Unrendered templates are always disabled
   */
  enabled: false;
  /**
   * Reusable template with typed parameters and a JSON definition template.
   */
  template: TemplateDefinitionOutput;
  /**
   * Partial or empty parameter values
   */
  templateValues?: { [k: string]: TemplateValue } | undefined;
};

/** @internal */
export const UnrenderedTemplateRule$inboundSchema: z.ZodMiniType<
  UnrenderedTemplateRule,
  unknown
> = z.pipe(
  z.object({
    enabled: z._default(types.literal(false), false),
    template: TemplateDefinitionOutput$inboundSchema,
    template_values: types.optional(
      z.record(z.string(), TemplateValue$inboundSchema),
    ),
  }),
  z.transform((v) => {
    return remap$(v, {
      "template_values": "templateValues",
    });
  }),
);

export function unrenderedTemplateRuleFromJSON(
  jsonString: string,
): SafeParseResult<UnrenderedTemplateRule, SDKValidationError> {
  return safeParse(
    jsonString,
    (x) => UnrenderedTemplateRule$inboundSchema.parse(JSON.parse(x)),
    `Failed to parse 'UnrenderedTemplateRule' from JSON`,
  );
}
