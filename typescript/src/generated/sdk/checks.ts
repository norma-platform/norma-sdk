// Modified for Norma.
import { checksCheck } from "../funcs/checks-check.js";
import { ClientSDK, RequestOptions } from "../lib/sdks.js";
import * as models from "../models/index.js";
import { unwrapAsync } from "../types/fp.js";

/** Hosted Agent Key operations; authorization remains server-enforced. */
export class Checks extends ClientSDK {
  async check(
    request: models.CheckRequest,
    options?: RequestOptions,
  ): Promise<models.CheckResponse> {
    return unwrapAsync(checksCheck(
      this,
      request,
      options,
    ));
  }
}
