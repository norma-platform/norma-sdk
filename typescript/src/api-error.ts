// Modified for Norma.
/** Safe, shared HTTP failure contract for every Norma SDK API. */
type ErrorMetadata = {
  body?: unknown;
  requestId?: unknown;
  requestURL?: string;
};

type PublicProblem = {
  type: string;
  title: string;
  status: number;
  code: string;
  detail: string;
  hint: string;
  request_id?: string;
  retry_after?: string;
};

const diagnosticCodes = new Set([
  "RESOURCE_NOT_FOUND", "AGENT_NOT_FOUND", "POLICY_NOT_FOUND", "RULE_NOT_FOUND",
  "RULE_VERSION_NOT_FOUND", "RULE_BINDING_NOT_FOUND", "EVALUATOR_NOT_FOUND",
  "AGENT_NAME_CONFLICT", "POLICY_NAME_CONFLICT", "RULE_NAME_CONFLICT",
  "RULE_BINDING_CONFLICT", "EVALUATOR_NAME_CONFLICT", "RULE_IN_USE",
  "RULE_TEMPLATE_CONFLICT", "EVALUATOR_IN_USE", "SCHEMA_INCOMPATIBLE",
  "VALIDATION_ERROR", "INVALID_CONFIG", "INVALID_SCHEMA", "CORRUPTED_DATA",
  "POLICY_RULE_INCOMPATIBLE", "RULE_BINDING_INCOMPATIBLE",
  "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR",
]);
const authenticationCodes = new Set(["AUTH_MISSING_KEY", "AUTH_INVALID_KEY", "AUTH_UPSTREAM_REJECTED"]);
const serviceCodes = new Set(["DATABASE_ERROR", "INTERNAL_ERROR", "CHECK_FAILED", "AUTH_MISCONFIGURED", "AUTH_UPSTREAM_REJECTED"]);

function object(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function payload(value: unknown): Record<string, unknown> {
  if (typeof value !== "string") return object(value);
  try {
    return object(JSON.parse(value));
  } catch {
    return {};
  }
}

function requestId(value: unknown): string | null {
  if (typeof value !== "string") return null;
  // Norma's gateway uses UUIDs and the direct runtime uses UUID hex strings.
  return /^(?:[a-f\d]{32}|[a-f\d]{64}|[a-f\d]{8}(?:-[a-f\d]{4}){3}-[a-f\d]{12})$/i.test(value)
    ? value
    : null;
}

function retryAfter(value: unknown): string | null {
  if (typeof value !== "string") return null;
  if (/^\d{1,10}$/.test(value) && Number(value) <= 2_147_483_647) return String(Number(value));
  if (!/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(value)) return null;
  const date = new Date(value);
  return Number.isFinite(date.getTime()) && date.toUTCString() === value ? value : null;
}

function knownCode(source: Record<string, unknown>, status: number): string | undefined {
  const allowed = status === 401 ? authenticationCodes
    : status >= 500 ? serviceCodes
    : [400, 404, 409, 422].includes(status) ? diagnosticCodes
    : undefined;
  if (!allowed) return undefined;
  const candidates = [source.code, source.type, source.error_code];
  for (const value of candidates) {
    if (typeof value !== "string") continue;
    const code = value.startsWith("NORMA_") ? value.slice(6)
      : value.startsWith("urn:norma:error:") ? value.slice(16).toUpperCase().replaceAll("-", "_")
      : value;
    if (allowed.has(code)) return `NORMA_${code}`;
  }
  return undefined;
}

function isAgentSetup(source: Record<string, unknown>, url?: string): boolean {
  let path = source.instance;
  if (url) {
    try {
      path = new URL(url, "https://norma.invalid").pathname;
    } catch {
      path = undefined;
    }
  }
  return path === "/api/v1/agents/register" || path === "/api/runtime/api/v1/agents/register";
}

function problem(status: number, source: Record<string, unknown>, url?: string): PublicProblem {
  let slug = "request-failed";
  let title = "Request failed";
  let detail = "Norma could not complete this request.";
  let hint = "Check the request and try again.";
  if (status === 401) {
    slug = "authentication-required";
    title = "Authentication required";
    detail = "A valid Norma API key is required.";
    hint = "Use an active key from Console → Agent Keys and configure it as agentKey.";
  } else if (status === 403) {
    slug = "access-denied";
    title = "Access denied";
    detail = "This credential does not allow the requested Norma action.";
    hint = isAgentSetup(source, url)
      ? 'Register the agent in Console → Agent Keys first, then initialize the permitted agent with conflictMode: "strict". Creating agents or replacing their setup requires the signed-in workspace owner in Console.'
      : "Check that the key allows this agent. Use the Norma Console for workspace setup.";
  } else if (status >= 500) {
    slug = "runtime-unavailable";
    title = "Norma service unavailable";
    detail = "Norma could not complete this request. Please retry.";
    hint = "If this continues, contact the workspace owner with the response request ID.";
  } else if (status === 404) {
    slug = "not-found";
    title = "Not found";
    detail = "The requested Norma resource is not available.";
    hint = "Check the resource name and that it belongs to this workspace.";
  } else if (status === 405) {
    slug = "method-not-allowed";
    title = "Method not allowed";
    detail = "This HTTP method is not available for the Norma endpoint.";
    hint = "Check the endpoint and HTTP method in your request.";
  } else if (status === 409) {
    slug = "conflict";
    title = "Configuration conflict";
    detail = "The request conflicts with the current Norma configuration.";
    hint = "Review the existing configuration before retrying.";
  } else if (status === 400 || status === 422) {
    slug = "validation-error";
    title = "Invalid request";
    detail = "Norma could not validate this request.";
    hint = "Check the request fields and their expected types.";
  } else if (status === 429) {
    slug = "rate-limited";
    title = "Request limit reached";
    detail = "The Norma request limit has been reached.";
    hint = "Wait before retrying and check the workspace usage allowance.";
  }
  const code = knownCode(source, status) ?? `NORMA_${slug.toUpperCase().replaceAll("-", "_")}`;
  return { type: `urn:norma:error:${code.slice(6).toLowerCase().replaceAll("_", "-")}`, title, status, code, detail, hint };
}

/** HTTP failure with stable Norma identifiers and safe, actionable diagnostics. */
export class NormaAPIError extends Error {
  readonly statusCode: number;
  readonly status: number;
  readonly status_code: number;
  readonly code: string;
  readonly type: string;
  readonly hint: string;
  readonly requestId: string | null;
  readonly request_id: string | null;
  readonly retryAfter: string | null;
  readonly retry_after: string | null;
  #problem: PublicProblem;

  constructor(statusCode: number, retryAfterValue: string | null = null, metadata: ErrorMetadata = {}) {
    const source = payload(metadata.body);
    const safe = problem(statusCode, source, metadata.requestURL);
    super(`${safe.detail} (HTTP ${statusCode}; ${safe.code}) ${safe.hint}`);
    this.name = "NormaAPIError";
    this.statusCode = this.status = this.status_code = statusCode;
    this.code = safe.code;
    this.type = safe.type;
    this.hint = safe.hint;
    this.requestId = this.request_id = requestId(metadata.requestId)
      ?? requestId(source.request_id)
      ?? requestId(object(source.metadata).request_id);
    this.retryAfter = this.retry_after = retryAfter(retryAfterValue);
    this.#problem = safe;
  }

  /** Body may be parsed JSON or text; its prose is never copied into diagnostics. */
  static fromResponse(response: Response, body?: unknown, requestURL?: string): NormaAPIError {
    return new NormaAPIError(response.status, response.headers.get("retry-after"), {
      body,
      requestId: response.headers.get("x-request-id"),
      requestURL: requestURL ?? (response.url || undefined),
    });
  }

  /** Serializing an error exposes only the normalized public problem. */
  toJSON(): PublicProblem {
    return {
      ...this.#problem,
      ...(this.requestId ? { request_id: this.requestId } : {}),
      ...(this.retryAfter ? { retry_after: this.retryAfter } : {}),
    };
  }
}
