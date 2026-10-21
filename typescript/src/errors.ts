// Modified for Norma.
import type { Decision } from "./public-api";
import { NormaAPIError } from "./api-error";
import type { RecoveryFeedback } from "./internal/semantic-check";

export type RuleDecision = "deny" | "steer" | "observe";

/** Response details only. A post-stage failure occurs after the action ran. */
export interface RuleFailureContext {
  stage?: "pre" | "post";
  recoveryFeedback?: RecoveryFeedback[] | null;
}

type RuleViolationParams = RuleFailureContext & {
  action: RuleDecision; decision: Decision; message?: string;
  ruleName: string; ruleId: string; checkId?: string;
};

export class RuleViolationError extends Error {
  readonly ruleName: string;
  readonly ruleId: string;
  readonly checkId?: string;
  readonly action: RuleDecision;
  readonly decision: Decision;
  readonly stage?: "pre" | "post";
  readonly recoveryFeedback?: RecoveryFeedback[] | null;

  constructor(params: RuleViolationParams) {
    const { ruleName, ruleId, checkId } = params;
    super(params.message ?? `Rule violation: ${ruleName}`);
    this.name = "RuleViolationError";
    this.ruleName = ruleName;
    this.ruleId = ruleId;
    this.checkId = checkId;
    this.action = params.action;
    this.decision = params.decision;
    this.stage = params.stage;
    this.recoveryFeedback = params.recoveryFeedback;
  }
}

/** An check failure; transport errors have no response feedback or IDs. */
export class CheckError extends Error {
  get code(): string { return this.cause instanceof NormaAPIError ? this.cause.code : "NORMA_CHECK_FAILED"; }
  get hint(): string { return this.cause instanceof NormaAPIError ? this.cause.hint : "Review the failed check in Norma before retrying."; }
  get statusCode(): number | undefined { return this.cause instanceof NormaAPIError ? this.cause.statusCode : undefined; }
  get status(): number | undefined { return this.statusCode; }
  get status_code(): number | undefined { return this.statusCode; }
  get requestId(): string | null { return this.cause instanceof NormaAPIError ? this.cause.requestId : null; }
  get request_id(): string | null { return this.requestId; }
  get retryAfter(): string | null { return this.cause instanceof NormaAPIError ? this.cause.retryAfter : null; }
  get retry_after(): string | null { return this.retryAfter; }

  readonly stage?: "pre" | "post";
  readonly recoveryFeedback?: RecoveryFeedback[] | null;
  readonly checkIds?: string[];
  readonly decision?: Decision;

  constructor(params: RuleFailureContext & {
    message: string;
    checkIds?: string[];
    decision?: Decision;
    cause?: unknown;
  }) {
    super(params.message, { cause: params.cause });
    this.name = "CheckError";
    this.stage = params.stage;
    this.recoveryFeedback = params.recoveryFeedback;
    this.checkIds = params.checkIds;
    this.decision = params.decision;
  }
}

/** A rule requires the caller to revise the proposed action before retrying. */
export class RuleSteerError extends RuleViolationError {
  constructor(params: ConstructorParameters<typeof RuleViolationError>[0]) {
    super({ ...params, action: "steer" });
    this.name = "RuleSteerError";
  }
}
