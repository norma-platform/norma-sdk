// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { ClosedEnum } from "../types/enums.js";

/**
 * Conflict handling mode for register registration updates.
 *
 * @remarks
 *
 * STRICT preserves compatibility checks and raises conflicts on incompatible changes.
 * OVERWRITE applies latest-init-wins replacement for steps and evaluators.
 */
export const ConflictMode = {
  Strict: "strict",
  Overwrite: "overwrite",
} as const;
/**
 * Conflict handling mode for register registration updates.
 *
 * @remarks
 *
 * STRICT preserves compatibility checks and raises conflicts on incompatible changes.
 * OVERWRITE applies latest-init-wins replacement for steps and evaluators.
 */
export type ConflictMode = ClosedEnum<typeof ConflictMode>;

/** @internal */
export const ConflictMode$outboundSchema: z.ZodMiniEnum<typeof ConflictMode> = z
  .enum(ConflictMode);
