// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { NormaSDKCore } from "../core.js";
import { encodeFormQuery, encodeSimple } from "../lib/encodings.js";
import * as M from "../lib/matchers.js";
import { compactMap } from "../lib/primitives.js";
import { safeParse } from "../lib/schemas.js";
import { RequestOptions } from "../lib/sdks.js";
import { extractSecurity, resolveGlobalSecurity } from "../lib/security.js";
import { pathToFunc } from "../lib/url.js";
import { NormaSDKError } from "../models/errors/norma-sdk-error.js";
import {
  ConnectionError,
  InvalidRequestError,
  RequestAbortedError,
  RequestTimeoutError,
  UnexpectedClientError,
} from "../models/errors/http-client-errors.js";
import * as errors from "../models/errors/index.js";
import { ResponseValidationError } from "../models/errors/response-validation-error.js";
import { SDKValidationError } from "../models/errors/sdk-validation-error.js";
import * as models from "../models/index.js";
import * as operations from "../models/operations/index.js";
import { APICall, APIPromise } from "../types/async.js";
import { Result } from "../types/fp.js";

/**
 * List agent's associated rules
 *
 * @remarks
 * List protection rules effective for an agent.
 *
 * The effective set is the de-duplicated union of the agent's direct
 * rules, policy-derived rules, and (when ``target_type`` and
 * ``target_id`` are both supplied) rules attached to that target via
 * enabled bindings in the same namespace. The same merge applies on
 * ``register`` and ``POST /checks`` so all three surfaces return the
 * same set for the same inputs.
 *
 * By default, the endpoint returns all effective Rules, including enabled
 * and disabled Rules. Callers can narrow the response with enabled_state.
 *
 * Args:
 *     agent_name: Agent identifier
 *     enabled_state: Whether to return enabled rules, disabled rules, or both
 *     target_type: Optional opaque target kind (paired with target_id)
 *     target_id: Optional opaque target identifier (paired with target_type)
 *
 * Returns:
 *     AgentRulesResponse with rules matching the requested state filters
 *
 * Raises:
 *     HTTPException 400: target_type and target_id were not supplied together
 *     HTTPException 404: Agent not found
 */
export function agentsListRules(
  client: NormaSDKCore,
  request: operations.ListAgentRulesApiV1AgentsAgentNameRulesGetRequest,
  options?: RequestOptions,
): APIPromise<
  Result<
    models.AgentRulesResponse,
    | errors.HTTPValidationError
    | NormaSDKError
    | ResponseValidationError
    | ConnectionError
    | RequestAbortedError
    | RequestTimeoutError
    | InvalidRequestError
    | UnexpectedClientError
    | SDKValidationError
  >
> {
  return new APIPromise($do(
    client,
    request,
    options,
  ));
}

async function $do(
  client: NormaSDKCore,
  request: operations.ListAgentRulesApiV1AgentsAgentNameRulesGetRequest,
  options?: RequestOptions,
): Promise<
  [
    Result<
      models.AgentRulesResponse,
      | errors.HTTPValidationError
      | NormaSDKError
      | ResponseValidationError
      | ConnectionError
      | RequestAbortedError
      | RequestTimeoutError
      | InvalidRequestError
      | UnexpectedClientError
      | SDKValidationError
    >,
    APICall,
  ]
> {
  const parsed = safeParse(
    request,
    (value) =>
      z.parse(
        operations
          .ListAgentRulesApiV1AgentsAgentNameRulesGetRequest$outboundSchema,
        value,
      ),
    "Input validation failed",
  );
  if (!parsed.ok) {
    return [parsed, { status: "invalid" }];
  }
  const payload = parsed.value;
  const body = null;

  const pathParams = {
    agent_name: encodeSimple("agent_name", payload.agent_name, {
      explode: false,
      charEncoding: "percent",
    }),
  };

  const path = pathToFunc("/api/v1/agents/{agent_name}/rules")(pathParams);

  const query = encodeFormQuery({
    "enabled_state": payload.enabled_state,
    "target_id": payload.target_id,
    "target_type": payload.target_type,
  });

  const headers = new Headers(compactMap({
    Accept: "application/json",
  }));

  const secConfig = await extractSecurity(client._options.apiKeyHeader);
  const securityInput = secConfig == null ? {} : { apiKeyHeader: secConfig };
  const requestSecurity = resolveGlobalSecurity(securityInput);

  const context = {
    options: client._options,
    baseURL: options?.serverURL ?? client._baseURL ?? "",
    operationID: "list_agent_rules_api_v1_agents__agent_name__rules_get",
    oAuth2Scopes: null,

    resolvedSecurity: requestSecurity,

    securitySource: client._options.apiKeyHeader,
    retryConfig: options?.retries
      || client._options.retryConfig
      || { strategy: "none" },
    retryCodes: options?.retryCodes || ["429", "500", "502", "503", "504"],
  };

  const requestRes = client._createRequest(context, {
    security: requestSecurity,
    method: "GET",
    baseURL: options?.serverURL,
    path: path,
    headers: headers,
    query: query,
    body: body,
    userAgent: client._options.userAgent,
    timeoutMs: options?.timeoutMs || client._options.timeoutMs || -1,
  }, options);
  if (!requestRes.ok) {
    return [requestRes, { status: "invalid" }];
  }
  const req = requestRes.value;

  const doResult = await client._do(req, {
    context,
    errorCodes: ["422", "4XX", "5XX"],
    retryConfig: context.retryConfig,
    retryCodes: context.retryCodes,
  });
  if (!doResult.ok) {
    return [doResult, { status: "request-error", request: req }];
  }
  const response = doResult.value;

  const responseFields = {
    HttpMeta: { Response: response, Request: req },
  };

  const [result] = await M.match<
    models.AgentRulesResponse,
    | errors.HTTPValidationError
    | NormaSDKError
    | ResponseValidationError
    | ConnectionError
    | RequestAbortedError
    | RequestTimeoutError
    | InvalidRequestError
    | UnexpectedClientError
    | SDKValidationError
  >(
    M.json(200, models.AgentRulesResponse$inboundSchema),
    M.jsonErr(422, errors.HTTPValidationError$inboundSchema),
    M.fail("4XX"),
    M.fail("5XX"),
  )(response, req, { extraFields: responseFields });
  if (!result.ok) {
    return [result, { status: "complete", request: req, response }];
  }

  return [result, { status: "complete", request: req, response }];
}
