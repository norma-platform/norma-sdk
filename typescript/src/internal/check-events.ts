// Modified for Norma.
/** Observed rule results only: these events are not receipts for external actions. */
import type { RuleCheckEvent } from "../generated/models/rule-check-event";
import type { CheckRequest } from "../generated/models/check-request";
import type { CheckResponse } from "../generated/models/check-response";

const debugKeys = new Set([
  "selected_data", "selected_data_preview", "engine_selected_data", "engine_selected_data_preview",
]);

function safeValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(safeValue);
  if (!value || typeof value !== "object") return value;
  return Object.fromEntries(Object.entries(value).flatMap(([key, item]) => {
    if (debugKeys.has(key)) return [];
    if (key === "semantic_observation") {
      if (!item || typeof item !== "object") return [];
      const observation = item as Record<string, unknown>;
      return [[key, Object.fromEntries(["observation_id", "check_release", "status"]
        .filter((name) => name in observation).map((name) => [name, observation[name]]))]];
    }
    return [[key, safeValue(item)]];
  }));
}

export function buildCheckEvents(
  request: CheckRequest,
  response: CheckResponse,
): RuleCheckEvent[] {
  const timestamp = new Date();
  // Correlation IDs are deliberately absent rather than invented request evidence.
  // Every result without a check ID receives a fresh occurrence ID once, before transport retries.
  return [
    ...(response.matches ?? []).map((match) => ({ match, matched: true })),
    ...(response.errors ?? []).map((match) => ({ match, matched: false })),
    ...(response.nonMatches ?? []).map((match) => ({ match, matched: false })),
    ...(response.unresolved ?? []).map((match) => ({ match, matched: false })),
  ].map(({ match, matched }) => {
    const source = match.result.metadata ?? {};
    const metadata = safeValue(source) as Record<string, unknown>;
    metadata.execution_id_source = match.checkId ? "engine" : "sdk_occurrence";
    if (match.result.message != null) metadata.reason = match.result.message;
    if (!("input" in metadata)) {
      for (const key of ["engine_selected_data_preview", "selected_data_preview"]) {
        const preview: unknown = source[key];
        if (preview && typeof preview === "object" && "value" in preview) {
          metadata.input = safeValue(preview.value);
          break;
        }
      }
    }
    metadata.rule_mode = match.mode ?? "enforce";
    metadata.rule_mode_contract = "rule-mode-v1";
    metadata.configured_action = match.action;
    if (match.result.outcome != null) {
      metadata.outcome = match.result.outcome;
      metadata.configured_action = match.action;
      metadata.failed_condition_ids = match.result.failedConditionIds ?? [];
      metadata.unresolved_evidence_ids = match.result.unresolvedEvidenceIds ?? [];
    }
    const observation = match.result.semanticObservation;
    if (observation) {
      metadata.semantic_observation_id = observation.observationId;
      metadata.check_release = observation.checkRelease;
    }
    if (response.controlContract === "rule-controls-v1") {
      const evidence = response.controlEvidence?.filter((item) => item.ruleId === match.ruleId) ?? [];
      metadata.control_contract = response.controlContract;
      if (response.agentGeneration) metadata.agent_generation = response.agentGeneration;
      if (evidence.length) {
        // Observation events carry disposition and identity only. Frozen control
        // values and raw numeric observations stay on the check response.
        metadata.control_evidence = evidence.map((item) => ({
          completeness: item.completeness,
          definition_fingerprint: item.definitionFingerprint,
          references: item.references.map((reference) => ({
            destination_pointer: reference.destinationPointer,
            disposition: reference.disposition,
            ...(reference.unavailableReason ? { unavailable_reason: reference.unavailableReason } : {}),
            numeric_comparisons: reference.numericComparisons.map((comparison) => ({
              field_path: comparison.fieldPath,
              operator: comparison.operator,
              outcome: comparison.outcome,
              ...(comparison.observedState ? { observed_state: comparison.observedState } : {}),
              ...(comparison.reason ? { reason: comparison.reason } : {}),
            })),
            ...(reference.numericComparisonsTruncated === undefined
              ? {} : { numeric_comparisons_truncated: reference.numericComparisonsTruncated }),
          })),
        }));
      }
    }
    return {
      checkId: match.checkId || globalThis.crypto.randomUUID(),
      traceId: "0".repeat(32), spanId: "0".repeat(16),
      agentName: request.agentName, ruleId: match.ruleId, ruleName: match.ruleName,
      checkStage: request.stage, appliesTo: `${request.step.type}_call`,
      action: match.effectiveAction ?? match.action, matched,
      confidence: match.result.confidence, timestamp,
      errorMessage: match.result.error, metadata,
    };
  });
}

export interface DeliveryReceipt {
  status: "delivered" | "acknowledged" | "pending" | "failed" | "empty" | "disabled" | "unconfirmed";
  acknowledged: number;
  /** Confirmed newly stored observations; null if any server receipt omitted this count. */
  persisted: number | null;
  duplicates: number | null;
  pending: number;
  dropped: number;
  /** Failed delivery attempts; a lost response does not prove the server stored nothing. */
  failed: number;
  unconfirmed: number;
  message: string | null;
}

/** Bounded client-owned reporting queue; reporting never changes enforcement. */
export class CheckReportQueue {
  private readonly batches: Array<{ events: RuleCheckEvent[]; send: (events: RuleCheckEvent[]) => Promise<unknown> }> = [];
  private eventCount = 0;
  private pending: Promise<void> | undefined;
  private acknowledged = 0;
  private persisted: number | null = 0;
  private duplicates: number | null = 0;
  private dropped = 0;
  private failed = 0;
  private unconfirmed = 0;

  enqueue(events: RuleCheckEvent[], send: (events: RuleCheckEvent[]) => Promise<unknown>): void {
    if (!events.length) return;
    if (this.eventCount + events.length > 1000) { this.dropped += events.length; return; }
    this.eventCount += events.length;
    this.batches.push({ events, send });
    if (!this.pending) this.pending = Promise.resolve().then(() => this.drain());
  }

  reportingFailed(count: number): void { this.failed += count; }

  /** Lifetime delivery snapshot; repeated flushes never count an event twice. */
  async flush(disabled = false): Promise<DeliveryReceipt> {
    while (this.pending) await this.pending;
    const status: DeliveryReceipt["status"] = this.eventCount ? "pending"
      : this.failed || this.dropped ? "failed"
      : this.unconfirmed ? "unconfirmed"
      : this.acknowledged ? this.persisted === null || this.duplicates === null ? "acknowledged" : "delivered"
      : disabled ? "disabled" : "empty";
    return { status, acknowledged: this.acknowledged, persisted: this.persisted,
      duplicates: this.duplicates, pending: this.eventCount, dropped: this.dropped,
      failed: this.failed, unconfirmed: this.unconfirmed,
      message: status === "acknowledged" ? "The server acknowledged observations; persistence is not confirmed."
        : status === "failed" ? "Some observation deliveries failed or could not be confirmed; check results are unchanged."
        : status === "unconfirmed" ? "The server did not provide a complete delivery receipt."
        : null };
  }

  private recordReceipt(value: unknown, count: number): void {
    if (!value || typeof value !== "object") { this.unconfirmed += count; return; }
    const receipt = value as Record<string, unknown>;
    const isCount = (value: unknown): value is number => Number.isInteger(value) && typeof value === "number" && value >= 0 && value <= count;
    if (!isCount(receipt.enqueued) || !isCount(receipt.dropped) || receipt.received !== count ||
      receipt.enqueued + receipt.dropped !== count || !["queued", "partial", "failed"].includes(String(receipt.status)) ||
      (receipt.status === "queued" && receipt.dropped !== 0) || (receipt.status === "failed" && receipt.enqueued !== 0)) {
      this.unconfirmed += count; return;
    }
    this.acknowledged += receipt.enqueued;
    this.dropped += receipt.dropped;
    if (!receipt.enqueued) return;
    if (receipt.stored == null || receipt.duplicates == null) {
      this.persisted = null; this.duplicates = null; return;
    }
    if (!isCount(receipt.stored) || !isCount(receipt.duplicates) || receipt.stored + receipt.duplicates !== receipt.enqueued) {
      this.persisted = null; this.duplicates = null; this.unconfirmed += receipt.enqueued; return;
    }
    if (this.persisted !== null) this.persisted += receipt.stored;
    if (this.duplicates !== null) this.duplicates += receipt.duplicates;
  }

  private async drain(): Promise<void> {
    try {
      let batch;
      while ((batch = this.batches.shift())) {
        try {
          this.recordReceipt(await batch.send(batch.events), batch.events.length);
        } catch (error) {
          const status = error && typeof error === "object" && "statusCode" in error ? error.statusCode : null;
          if (typeof status === "number" && status >= 200 && status < 300) this.unconfirmed += batch.events.length;
          else this.failed += batch.events.length;
        } finally {
          this.eventCount -= batch.events.length;
        }
      }
    } finally { this.pending = undefined; }
  }
}
