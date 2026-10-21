// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import * as types from "../../types/primitives.js";
import * as models from "../index.js";
import { NormaSDKError } from "./norma-sdk-error.js";

export type HTTPValidationErrorData = {
  detail?: Array<models.ValidationError> | undefined;
};

export class HTTPValidationError extends NormaSDKError {
  detail?: Array<models.ValidationError> | undefined;

  /** The original data that was passed to this error instance. */
  data$: HTTPValidationErrorData;

  constructor(
    err: HTTPValidationErrorData,
    httpMeta: { response: Response; request: Request; body: string },
  ) {
    const message = "message" in err && typeof err.message === "string"
      ? err.message
      : `API error occurred: ${JSON.stringify(err)}`;
    super(message, httpMeta);
    this.data$ = err;
    if (err.detail != null) this.detail = err.detail;

    this.name = "HTTPValidationError";
  }
}

/** @internal */
export const HTTPValidationError$inboundSchema: z.ZodMiniType<
  HTTPValidationError,
  unknown
> = z.pipe(
  z.object({
    detail: types.optional(z.array(models.ValidationError$inboundSchema)),
    request$: z.custom<Request>(x => x instanceof Request),
    response$: z.custom<Response>(x => x instanceof Response),
    body$: z.string(),
  }),
  z.transform((v) => {
    return new HTTPValidationError(v, {
      request: v.request$,
      response: v.response$,
      body: v.body$,
    });
  }),
);
