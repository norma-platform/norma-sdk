// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import { NormaSDKError } from "./norma-sdk-error.js";

/** The fallback error class if no more specific error class is matched. */
export class NormaSDKDefaultError extends NormaSDKError {
  constructor(
    message: string,
    httpMeta: {
      response: Response;
      request: Request;
      body: string;
    },
  ) {
    super(message, httpMeta);
    this.name = "NormaSDKDefaultError";
  }
}
