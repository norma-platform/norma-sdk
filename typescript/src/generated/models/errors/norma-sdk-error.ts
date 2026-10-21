// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import { NormaAPIError } from "../../../api-error.js";

/** The base class for all HTTP error responses. */
export class NormaSDKError extends NormaAPIError {
  /** Serialized, normalized Norma problem; raw upstream prose is excluded. */
  public readonly body: string;
  /** HTTP headers, retained for compatibility. */
  public readonly headers: Headers;
  /** HTTP content type */
  public readonly contentType: string;
  /** Original response for callers explicitly inspecting transport diagnostics. */
  public readonly rawResponse: Response;

  constructor(
    _message: string,
    httpMeta: {
      response: Response;
      request: Request;
      body: string;
    },
  ) {
    super(httpMeta.response.status, httpMeta.response.headers.get("retry-after"), {
      body: httpMeta.body,
      requestId: httpMeta.response.headers.get("x-request-id"),
      requestURL: httpMeta.request.url,
    });
    this.body = JSON.stringify(this.toJSON());
    this.headers = httpMeta.response.headers;
    this.contentType = httpMeta.response.headers.get("content-type") || "";
    this.rawResponse = httpMeta.response;
    this.name = "NormaSDKError";
  }
}
