// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import * as openEnums from "../types/enums.js";
import { OpenEnum } from "../types/enums.js";

export const ActionDecision = {
  Deny: "deny",
  Steer: "steer",
  Observe: "observe",
} as const;
export type ActionDecision = OpenEnum<typeof ActionDecision>;

/** @internal */
export const ActionDecision$inboundSchema: z.ZodMiniType<
  ActionDecision,
  unknown
> = openEnums.inboundSchema(ActionDecision);
/** @internal */
export const ActionDecision$outboundSchema: z.ZodMiniType<
  string,
  ActionDecision
> = openEnums.outboundSchema(ActionDecision);
