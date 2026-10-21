// Modified for Norma.
import { evaluatorsList } from "../funcs/evaluators-list.js";
import { ClientSDK, RequestOptions } from "../lib/sdks.js";
import * as models from "../models/index.js";
import { unwrapAsync } from "../types/fp.js";

/** Hosted Agent Key operations; authorization remains server-enforced. */
export class Evaluators extends ClientSDK {
  async list(
    options?: RequestOptions,
  ): Promise<{ [k: string]: models.EvaluatorInfo }> {
    return unwrapAsync(evaluatorsList(
      this,
      options,
    ));
  }
}
