// Modified for Norma.
import { observabilityIngestEvents } from "../funcs/observability-ingest-events.js";
import { ClientSDK, RequestOptions } from "../lib/sdks.js";
import * as models from "../models/index.js";
import * as operations from "../models/operations/index.js";
import { unwrapAsync } from "../types/fp.js";

/** Hosted Agent Key operations; authorization remains server-enforced. */
export class Observability extends ClientSDK {
  async ingestEvents(
    request: models.BatchEventsRequest,
    options?: RequestOptions,
  ): Promise<models.BatchEventsResponse> {
    return unwrapAsync(observabilityIngestEvents(
      this,
      request,
      options,
    ));
  }
}
