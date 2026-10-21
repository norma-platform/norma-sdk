// Modified for Norma.
/*
 * Public protocol snapshot, originally generated with Speakeasy (https://speakeasy.com).
 * Maintained with public-contract.json; builds never import a private server schema.
 */

import * as z from "zod/v4-mini";
import { NormaSDKCore } from "../core.js";
import { encodeJSON } from "../lib/encodings.js";
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
import { APICall, APIPromise } from "../types/async.js";
import { Result } from "../types/fp.js";

/**
 * Initialize or update an agent
 *
 * @remarks
 * Initialize compatible steps for an existing allowed agent. Creating an identity or replacing setup requires the signed-in workspace owner in Console.
 *
 * This endpoint is idempotent:
 * - If the agent name doesn't exist, creates a new agent
 * - If the agent name exists, updates registration data in place
 *
 * conflict_mode rules registration conflict handling:
 * - strict (default): preserve compatibility checks and conflict errors
 * - overwrite: latest init payload replaces steps/evaluators and returns change summary
 *
 * The returned ``rules`` list is the de-duplicated union of the agent's
 * direct rules, policy-derived rules, and (when ``target_type`` and
 * ``target_id`` are both supplied on the request) rules attached to that
 * target via enabled bindings in the same namespace. The same merge applies
 * on ``GET /agents/{name}/rules`` and ``POST /checks``. Bindings can
 * pre-exist the agent row, so a newly created agent that registers with
 * target context can observe target rules immediately.
 *
 * Args:
 *     request: Agent metadata and step schemas
 *
 * Returns:
 *     RegisterAgentResponse with created flag and the effective rules
 */
export function agentsRegister(
  client: NormaSDKCore,
  request: models.RegisterAgentRequest,
  options?: RequestOptions,
): APIPromise<
  Result<
    models.RegisterAgentResponse,
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
  request: models.RegisterAgentRequest,
  options?: RequestOptions,
): Promise<
  [
    Result<
      models.RegisterAgentResponse,
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
    (value) => z.parse(models.RegisterAgentRequest$outboundSchema, value),
    "Input validation failed",
  );
  if (!parsed.ok) {
    return [parsed, { status: "invalid" }];
  }
  const payload = parsed.value;
  const body = encodeJSON("body", payload, { explode: true });

  const path = pathToFunc("/api/v1/agents/register")();

  const headers = new Headers(compactMap({
    "Content-Type": "application/json",
    Accept: "application/json",
  }));

  const secConfig = await extractSecurity(client._options.apiKeyHeader);
  const securityInput = secConfig == null ? {} : { apiKeyHeader: secConfig };
  const requestSecurity = resolveGlobalSecurity(securityInput);

  const context = {
    options: client._options,
    baseURL: options?.serverURL ?? client._baseURL ?? "",
    operationID: "register_agent_api_v1_agents_register_post",
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
    method: "POST",
    baseURL: options?.serverURL,
    path: path,
    headers: headers,
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
    models.RegisterAgentResponse,
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
    M.json(200, models.RegisterAgentResponse$inboundSchema),
    M.jsonErr(422, errors.HTTPValidationError$inboundSchema),
    M.fail("4XX"),
    M.fail("5XX"),
  )(response, req, { extraFields: responseFields });
  if (!result.ok) {
    return [result, { status: "complete", request: req, response }];
  }

  return [result, { status: "complete", request: req, response }];
}
