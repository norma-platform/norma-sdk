// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import {
  RuleCheckEvent,
  RuleCheckEvent$Outbound,
  RuleCheckEvent$outboundSchema,
} from "./rule-check-event.js";

/**
 * Request model for batch event ingestion.
 *
 * @remarks
 *
 * SDKs batch events and send them to the server periodically.
 * This reduces HTTP overhead significantly (100x reduction).
 *
 * Attributes:
 *     events: List of rule execution events to ingest
 */
export type BatchEventsRequest = {
  /**
   * List of events to ingest
   */
  events: Array<RuleCheckEvent>;
};

/** @internal */
export type BatchEventsRequest$Outbound = {
  events: Array<RuleCheckEvent$Outbound>;
};

/** @internal */
export const BatchEventsRequest$outboundSchema: z.ZodMiniType<
  BatchEventsRequest$Outbound,
  BatchEventsRequest
> = z.object({
  events: z.array(RuleCheckEvent$outboundSchema),
});

export function batchEventsRequestToJSON(
  batchEventsRequest: BatchEventsRequest,
): string {
  return JSON.stringify(
    BatchEventsRequest$outboundSchema.parse(batchEventsRequest),
  );
}
