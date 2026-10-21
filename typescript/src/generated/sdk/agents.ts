// Modified for Norma.
import { agentsGetEvaluator } from "../funcs/agents-get-evaluator.js";
import { agentsGet } from "../funcs/agents-get.js";
import { agentsRegister } from "../funcs/agents-register.js";
import { agentsListRules } from "../funcs/agents-list-rules.js";
import { agentsListEvaluators } from "../funcs/agents-list-evaluators.js";
import { agentsList } from "../funcs/agents-list.js";
import { agentsUpdate } from "../funcs/agents-update.js";
import { ClientSDK, RequestOptions } from "../lib/sdks.js";
import * as models from "../models/index.js";
import * as operations from "../models/operations/index.js";
import { unwrapAsync } from "../types/fp.js";

/** Hosted Agent Key operations; authorization remains server-enforced. */
export class Agents extends ClientSDK {
  async list(
    request?: operations.ListAgentsApiV1AgentsGetRequest | undefined,
    options?: RequestOptions,
  ): Promise<models.ListAgentsResponse> {
    return unwrapAsync(agentsList(
      this,
      request,
      options,
    ));
  }

  async register(
    request: models.RegisterAgentRequest,
    options?: RequestOptions,
  ): Promise<models.RegisterAgentResponse> {
    return unwrapAsync(agentsRegister(
      this,
      request,
      options,
    ));
  }

  async get(
    request: operations.GetAgentApiV1AgentsAgentNameGetRequest,
    options?: RequestOptions,
  ): Promise<models.GetAgentResponse> {
    return unwrapAsync(agentsGet(
      this,
      request,
      options,
    ));
  }

  async update(
    request: operations.PatchAgentApiV1AgentsAgentNamePatchRequest,
    options?: RequestOptions,
  ): Promise<models.PatchAgentResponse> {
    return unwrapAsync(agentsUpdate(
      this,
      request,
      options,
    ));
  }

  async listRules(
    request: operations.ListAgentRulesApiV1AgentsAgentNameRulesGetRequest,
    options?: RequestOptions,
  ): Promise<models.AgentRulesResponse> {
    return unwrapAsync(agentsListRules(
      this,
      request,
      options,
    ));
  }

  async listEvaluators(
    request:
      operations.ListAgentEvaluatorsApiV1AgentsAgentNameEvaluatorsGetRequest,
    options?: RequestOptions,
  ): Promise<models.ListEvaluatorsResponse> {
    return unwrapAsync(agentsListEvaluators(
      this,
      request,
      options,
    ));
  }

  async getEvaluator(
    request:
      operations.GetAgentEvaluatorApiV1AgentsAgentNameEvaluatorsEvaluatorNameGetRequest,
    options?: RequestOptions,
  ): Promise<models.EvaluatorSchemaItem> {
    return unwrapAsync(agentsGetEvaluator(
      this,
      request,
      options,
    ));
  }
}
