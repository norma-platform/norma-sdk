// Modified for Norma.
import { ruleBindingsGet } from "../funcs/rule-bindings-get.js";
import { ruleBindingsList } from "../funcs/rule-bindings-list.js";
import { ClientSDK, RequestOptions } from "../lib/sdks.js";
import * as models from "../models/index.js";
import * as operations from "../models/operations/index.js";
import { unwrapAsync } from "../types/fp.js";

/** Hosted Agent Key operations; authorization remains server-enforced. */
export class RuleBindings extends ClientSDK {
  async list(
    request?:
      | operations.ListRuleBindingsApiV1RuleBindingsGetRequest
      | undefined,
    options?: RequestOptions,
  ): Promise<models.ListRuleBindingsResponse> {
    return unwrapAsync(ruleBindingsList(
      this,
      request,
      options,
    ));
  }

  async get(
    request:
      operations.GetRuleBindingApiV1RuleBindingsBindingIdGetRequest,
    options?: RequestOptions,
  ): Promise<models.GetRuleBindingResponse> {
    return unwrapAsync(ruleBindingsGet(
      this,
      request,
      options,
    ));
  }
}
