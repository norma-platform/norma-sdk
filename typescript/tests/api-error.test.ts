// Modified for Norma.
import { describe, expect, it } from "vitest";

import { NormaAPIError } from "../src/api-error";
import { NormaSDKError } from "../src/generated/models/errors/norma-sdk-error";
import { NormaSDKDefaultError } from "../src/generated/models/errors/norma-sdk-default-error";

const requestID = "9dd4f6f2-68de-43c1-9ca5-99752919671b";
const inheritedError = {
  type: "https://private.invalid/errors/auth-insufficient-privileges",
  status: 200,
  detail: "Private Runtime: Not authorized to perform 'agents.create'. synthetic-secret",
  error_code: "AUTH_INSUFFICIENT_PRIVILEGES",
  hint: "Ask Private Runtime for agents.create permission. synthetic-secret",
  instance: "/api/v1/agents/register",
};

function expectSafe(error: NormaAPIError): void {
  expect(`${error.message}\n${error.hint}\n${JSON.stringify(error)}`)
    .not.toMatch(/agent[ -]?rule|agents\.create|synthetic-secret/i);
}

describe("Norma API errors", () => {
  it.each(["RULE_TEMPLATE_CONFLICT", "TEMPLATE_PARAMETER_INVALID", "TEMPLATE_RENDER_ERROR"])(
    "does not recognize retired template diagnostic %s",
    (errorCode) => {
      const error = NormaAPIError.fromResponse(new Response(null, { status: 409 }), {
        error_code: errorCode,
      });
      expect(error.code).toBe("NORMA_CONFLICT");
      expect(error.type).toBe("urn:norma:error:conflict");
    },
  );

  it("retains only reviewed control diagnostic codes and source pointers", () => {
    const error = NormaAPIError.fromResponse(new Response(null, { status: 422 }), {
      error_code: "VALIDATION_ERROR",
      errors: [
        { field: "/controls/age/default", code: "control_value_out_of_bounds", message: "raw value 999" },
        { field: "/controls/age/~2bad", code: "control_reference_invalid" },
        { field: "/controls/age/default", code: "control_reference_invalid", message: "synthetic-secret" },
        { field: "/values", code: "control_evidence_size_exceeded", message: "private capacity detail" },
        { field: "/controls/age/default", code: "private_code" },
      ],
    });
    expect(error.diagnostics).toEqual([
      { code: "control_value_out_of_bounds", sourcePointer: "/controls/age/default" },
      { code: "control_reference_invalid", sourcePointer: "/controls/age/default" },
      { code: "control_evidence_size_exceeded", sourcePointer: "/values" },
    ]);
    expect(`${error.message}${JSON.stringify(error)}`).not.toContain("synthetic-secret");
    expect(`${error.message}${JSON.stringify(error)}`).not.toContain("private capacity detail");
  });
  it("normalizes an older runtime denial without changing its HTTP status", () => {
    // Given: a denied response carrying unsafe legacy prose and a forged body status.
    const response = new Response(JSON.stringify(inheritedError), { status: 403 });
    // When: the public error contract handles the response.
    const error = NormaAPIError.fromResponse(response, JSON.stringify(inheritedError));
    // Then: the denial and stable identifier survive while setup guidance is actionable.
    expect(error).toBeInstanceOf(Error);
    expect(error).toMatchObject({
      name: "NormaAPIError", status: 403, statusCode: 403, status_code: 403,
      code: "NORMA_ACCESS_DENIED", type: "urn:norma:error:access-denied",
    });
    expect(error.hint).toContain('conflictMode: "strict"');
    expect(error.message).toContain("Console → Agent Keys");
    expectSafe(error);
  });

  it.each([
    [401, "AUTH_INVALID_KEY", "NORMA_AUTH_INVALID_KEY"],
    [404, "AGENT_NOT_FOUND", "NORMA_AGENT_NOT_FOUND"],
    [409, "AGENT_NAME_CONFLICT", "NORMA_AGENT_NAME_CONFLICT"],
    [422, "INVALID_SCHEMA", "NORMA_INVALID_SCHEMA"],
    [500, "AUTH_MISCONFIGURED", "NORMA_AUTH_MISCONFIGURED"],
    [503, "DATABASE_ERROR", "NORMA_DATABASE_ERROR"],
  ] as const)("preserves the known %s diagnostic %s", (status, legacy, code) => {
    // Given: the same diagnostic in legacy and normalized response shapes.
    const type = `urn:norma:error:${code.slice(6).toLowerCase().replaceAll("_", "-")}`;
    const shapes = [{ error_code: legacy }, { code }, { type }];
    // When: each format becomes a public error.
    const errors = shapes.map((body) => NormaAPIError.fromResponse(new Response(null, { status }), {
      ...inheritedError, ...body, error_code: "error_code" in body ? body.error_code : undefined,
      type: "type" in body ? body.type : undefined,
    }));
    // Then: format differences cannot change the stable identifier or surface unsafe prose.
    for (const error of errors) {
      expect(error.code).toBe(code);
      expect(error.type).toBe(type);
      expect(error.status).toBe(status);
      expectSafe(error);
    }
  });

  it.each([
    [400, "NORMA_VALIDATION_ERROR"], [401, "NORMA_AUTHENTICATION_REQUIRED"],
    [403, "NORMA_ACCESS_DENIED"], [404, "NORMA_NOT_FOUND"],
    [405, "NORMA_METHOD_NOT_ALLOWED"], [409, "NORMA_CONFLICT"],
    [422, "NORMA_VALIDATION_ERROR"], [429, "NORMA_RATE_LIMITED"],
    [503, "NORMA_RUNTIME_UNAVAILABLE"],
  ] as const)("uses a status-based fallback for untrusted %s diagnostics", (status, code) => {
    // Given: an unknown server code and branded HTML body.
    const bodies = ["<html>Private Runtime synthetic-secret</html>", {
      code: "NORMA_synthetic-secret", type: "urn:norma:error:synthetic-secret",
      detail: inheritedError.detail, hint: inheritedError.hint,
    }];
    // When: the malformed or unknown response is normalized.
    const errors = bodies.map((body) => NormaAPIError.fromResponse(new Response(null, { status }), body));
    // Then: only reviewed identifiers and messages become public diagnostics.
    for (const error of errors) {
      expect(error.code).toBe(code);
      expectSafe(error);
    }
  });

  it("uses the actual request route for initialization guidance", () => {
    // Given: an older server has no useful body, but the caller knows its request URL.
    const response = new Response(null, { status: 403 });
    // When: normalizing the failed initialization.
    const error = NormaAPIError.fromResponse(response, undefined,
      "https://norma.invalid/api/runtime/api/v1/agents/register");
    // Then: setup guidance names the supported Console flow, not an unavailable setup key.
    expect(error.hint).toBe('Register the agent in Console → Agent Keys first, then initialize the permitted agent with conflictMode: "strict". Creating agents or replacing their setup requires the signed-in workspace owner in Console.');
    expect(error.hint).not.toMatch(/setup credential/i);
    expect(error.code).toBe("NORMA_ACCESS_DENIED");
  });

  it("retains safe request tracing and retry values in both naming conventions", () => {
    // Given: a rate limit with valid transport diagnostics and an unrelated body ID.
    const response = new Response(null, {
      status: 429, headers: { "x-request-id": requestID, "retry-after": "12" },
    });
    // When: the response becomes a typed error.
    const error = NormaAPIError.fromResponse(response, { request_id: "a".repeat(32) });
    // Then: trusted transport metadata takes precedence and stays available to callers.
    expect(error).toMatchObject({
      requestId: requestID, request_id: requestID, retryAfter: "12", retry_after: "12",
    });
    expect(JSON.parse(JSON.stringify(error))).toMatchObject({ request_id: requestID, retry_after: "12" });
  });

  it("accepts a direct-runtime request ID from metadata", () => {
    // Given: a direct runtime puts its tracing identifier in metadata.
    const id = "a".repeat(32);
    // When: converting its error without a request-ID header.
    const error = NormaAPIError.fromResponse(new Response(null, { status: 500 }), { metadata: { request_id: id } });
    // Then: safe tracing remains available.
    expect(error.requestId).toBe(id);
  });

  it.each(["-1", "Infinity", "1.5", "2147483648", "Private Runtime synthetic-secret", "Wed, 99 Oct 2015 07:28:00 GMT"])(
    "rejects unsafe retry timing %s", (value) => {
      // Given: an arbitrary Retry-After value from a server.
      // When: using the backwards-compatible status/retry constructor.
      const error = new NormaAPIError(429, value);
      // Then: no unknown server string becomes part of the public error.
      expect(error.retryAfter).toBeNull();
      expectSafe(error);
    },
  );

  it("preserves a valid HTTP-date retry value", () => {
    // Given: a valid RFC HTTP-date Retry-After header.
    const value = "Wed, 21 Oct 2015 07:28:00 GMT";
    // When: constructing the error.
    const error = new NormaAPIError(429, value);
    // Then: consumers can honor the original safe retry timestamp.
    expect(error.retryAfter).toBe(value);
  });

  it("does not echo arbitrary request identifiers", () => {
    // Given: an upstream trace field containing internal wording.
    const response = new Response(null, { status: 500, headers: { "x-request-id": "agents.create" } });
    // When: parsing transport and body identifiers.
    const error = NormaAPIError.fromResponse(response, { request_id: "PrivateRuntime-synthetic-secret" });
    // Then: only the runtime's known opaque identifier formats are retained.
    expect(error.requestId).toBeNull();
    expectSafe(error);
  });

  it.each([NormaSDKError, NormaSDKDefaultError])("normalizes generated %s error messages and body", (ErrorClass) => {
    // Given: generated errors receive both an inherited message and raw HTTP metadata.
    const body = JSON.stringify(inheritedError);
    const response = new Response(body, { status: 403 });
    const metadata = { body, response, request: new Request("https://norma.invalid/api/v1/agents/register") };
    // When: the generated class constructs its public diagnostics.
    const error = new ErrorClass(inheritedError.detail, metadata);
    // Then: class compatibility survives, while the public body is a normalized problem.
    expect(error).toBeInstanceOf(NormaAPIError);
    expect(error).toBeInstanceOf(NormaSDKError);
    expect(error.statusCode).toBe(403);
    expect(error.rawResponse).toBe(response);
    expect(JSON.parse(error.body)).toMatchObject({ status: 403, code: "NORMA_ACCESS_DENIED" });
    expect(error.body).not.toMatch(/agent[ -]?rule|agents\.create|synthetic-secret/i);
    expectSafe(error);
  });
});

describe("Rule identifiers across server versions", () => {
  it.each(["RULE_NOT_FOUND", "NORMA_RULE_NOT_FOUND", "NORMA_RULE_NOT_FOUND", "urn:norma:error:rule-not-found"])("normalizes %s to the public Rule code", (code) => {
    const error = NormaAPIError.fromResponse(new Response(null, { status: 404 }), { code });
    expect(error.code).toBe("NORMA_RULE_NOT_FOUND");
    expect(error.type).toBe("urn:norma:error:rule-not-found");
  });
});

describe("step schema registration recovery", () => {
  it.each([
    "https://norma.invalid/api/v1/agents/register",
    "https://console.invalid/api/runtime/api/v1/agents/register?agent_name=example",
  ])("shows the reviewed recovery for a Step schema conflict on %s", (url) => {
    // Given: the registration route and a recognized Step-specific conflict discriminator.
    const body = {
      code: "SCHEMA_INCOMPATIBLE",
      details: { kind: "Step" },
      detail: "Step schema conflict for private-step. synthetic-secret",
      hint: "Private internal prose synthetic-secret",
    };

    // When: the SDK normalizes the HTTP conflict.
    const error = NormaAPIError.fromResponse(new Response(null, { status: 409 }), body, url);

    // Then: it provides fixed Console recovery without exposing server-authored prose.
    expect(error.code).toBe("NORMA_SCHEMA_INCOMPATIBLE");
    expect(error.toJSON().title).toBe("Step schema conflict");
    expect(error.hint).toContain("Console → Agents → agent → Connection → Edit step schemas → Import schemas");
    expect(error.hint).toContain('conflictMode: "strict"');
    expectSafe(error);
  });

  it("recognizes the Step schema mismatch error shape only on agent registration", () => {
    // Given: a direct registration response with the server's validation discriminator.
    const body = {
      error_code: "SCHEMA_INCOMPATIBLE",
      errors: [{ resource: "Step", code: "schema_mismatch", message: "synthetic-secret" }],
    };

    // When: parsing the known registration endpoint.
    const error = NormaAPIError.fromResponse(
      new Response(null, { status: 409 }),
      body,
      "https://norma.invalid/api/v1/agents/register"
    );

    // Then: the specific recovery is selected without copying the error message.
    expect(error.toJSON().title).toBe("Step schema conflict");
    expect(error.hint).toContain("Import schemas");
    expectSafe(error);
  });

  it.each([
    ["https://norma.invalid/api/v1/rule-reviews", { detail: "Step schema conflict for review" }],
    ["https://norma.invalid/api/v1/agents/register", { detail: "Evaluator schema mismatch" }],
    ["https://norma.invalid/api/v1/agents/register", { details: { kind: "Evaluator" } }],
  ])("keeps unrelated SCHEMA_INCOMPATIBLE conflicts generic", (url, discriminator) => {
    // Given: a schema conflict without an accepted Step registration discriminator.
    const body = { code: "SCHEMA_INCOMPATIBLE", ...discriminator, hint: "synthetic-secret" };

    // When: normalizing the response.
    const error = NormaAPIError.fromResponse(new Response(null, { status: 409 }), body, url);

    // Then: callers get the safe generic conflict guidance.
    expect(error.toJSON().title).toBe("Configuration conflict");
    expect(error.hint).toBe("Review the existing configuration before retrying.");
    expectSafe(error);
  });
});
