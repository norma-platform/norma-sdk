// Modified for Norma.
/** Opt-in negotiation for versioned control receipts and evidence. */

export type ControlContract = "rule-controls-v1";
export const CONTROL_CONTRACT_HEADER = "X-Norma-Control-Contract";

/** A runtime did not prove it supports the requested control contract. */
export class UnsupportedControlContractError extends Error {
  readonly code = "CONTROL_CONTRACT_UNSUPPORTED";

  constructor() {
    super("Unsupported Norma control contract: the runtime must echo rule-controls-v1 in both the response header and envelope.");
    this.name = "UnsupportedControlContractError";
  }
}

/** Routes whose responses retain control-aware effective rules or decisions. */
export function isControlContractRoute(method: string, path: string): boolean {
  const verb = method.toUpperCase();
  if (verb === "POST" && ["/api/v1/agents/register", "/api/v1/checks", "/api/v1/operations"].includes(path)) return true;
  if ((verb === "GET" || verb === "POST") && /^\/api\/v1\/agents\/[^/]+\/rules$/.test(path)) return true;
  if (path === "/api/v1/operations/observe-metrics") return false;
  if ((verb === "POST" && path === "/api/v1/check-runs/shadow")
    || (verb === "GET" && /^\/api\/v1\/check-runs(?:\/[^/]+)?$/.test(path))) return true;
  return (verb === "GET" || verb === "POST") && /^\/api\/v1\/operations(?:\/[^/]+(?:\/(?:observations|verification))?)?$/.test(path);
}

/** Require both exact acknowledgements before an opted-in response is trusted. */
export async function requireControlAcknowledgement(response: Response): Promise<void> {
  if (response.headers.get(CONTROL_CONTRACT_HEADER) !== "rule-controls-v1") {
    throw new UnsupportedControlContractError();
  }
  let payload: unknown;
  try { payload = await response.clone().json(); } catch { throw new UnsupportedControlContractError(); }
  requireControlAcknowledgementEnvelope(response, payload);
}

/** Validate a body already parsed by a bounded reader, without cloning it. */
export function requireControlAcknowledgementEnvelope(response: Response, payload: unknown): void {
  if (response.headers.get(CONTROL_CONTRACT_HEADER) !== "rule-controls-v1") {
    throw new UnsupportedControlContractError();
  }
  if (!payload || typeof payload !== "object" || (payload as Record<string, unknown>).control_contract !== "rule-controls-v1") {
    throw new UnsupportedControlContractError();
  }
}
