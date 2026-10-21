// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";

export type Security = {
  apiKeyHeader?: string | undefined;
};

/** @internal */
export type Security$Outbound = {
  APIKeyHeader?: string | undefined;
};

/** @internal */
export const Security$outboundSchema: z.ZodMiniType<
  Security$Outbound,
  Security
> = z.pipe(
  z.object({
    apiKeyHeader: z.optional(z.string()),
  }),
  z.transform((v) => {
    return remap$(v, {
      apiKeyHeader: "APIKeyHeader",
    });
  }),
);

export function securityToJSON(security: Security): string {
  return JSON.stringify(Security$outboundSchema.parse(security));
}
