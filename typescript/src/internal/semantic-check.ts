// Modified for Norma.
/** Inbound semantic evidence contracts, independent of any inference provider. */
import * as z from "zod/v4-mini";
import { jsonValueSchema, recordSchema } from "../json-contract";

export const CheckOutcomeSchema = z.enum(["matched", "not_matched", "unresolved"]);
const evidenceOrigin = z.enum(["caller_asserted", "runtime_observed", "source_verified"]);
const evidenceStatus = z.enum(["present", "missing", "stale", "conflicting", "truncated", "redacted"]);
const probability = z.number().check(z.minimum(0), z.maximum(1));
const optionalText = z.optional(z.nullable(z.string()));
const optionalStrings = z.optional(z.array(z.string()));

const statementAnswer = z.object({ type: z.literal("statement"), support_score: probability });
const choiceAnswer = z.object({
  type: z.literal("choice"), choice: z.string(), confidence: probability,
  probabilities: recordSchema(probability),
});
const scoreAnswer = z.object({
  type: z.literal("score"), score: z.number().check(z.minimum(0)), confidence: probability,
  legend: recordSchema(z.string()), probabilities: recordSchema(probability),
});
export type SemanticAnswer = {type: "statement"; support_score: number} |
  {type: "choice"; choice: string; confidence: number; probabilities: Record<string, number>} |
  {type: "score"; score: number; confidence: number; legend: Record<string, string>; probabilities: Record<string, number>};
export const SemanticAnswerSchema: z.ZodMiniType<SemanticAnswer, unknown> = z.discriminatedUnion("type", [statementAnswer, choiceAnswer, scoreAnswer]);

export type CheckOutcome = z.infer<typeof CheckOutcomeSchema>;

const evidenceItem = z.pipe(z.object({
  name: z.string(), path: z.string(), value: z.optional(jsonValueSchema),
  required: z.optional(z.boolean()), origin: z.optional(evidenceOrigin), status: z.optional(evidenceStatus),
  source_id: optionalText, source_version: optionalText, observed_at: optionalText,
  fingerprint: z.string(), truncated: z.optional(z.boolean()),
}), z.transform(({ source_id, source_version, observed_at, ...rest }) => ({
  ...rest, sourceId: source_id, sourceVersion: source_version, observedAt: observed_at,
})));
export type SemanticEvidenceItem = z.infer<typeof evidenceItem>;

const evidenceBundle = z.pipe(z.object({
  items: z.optional(z.array(evidenceItem)), fingerprint: z.string(), complete: z.boolean(),
  missing_required: optionalStrings, unresolved: optionalStrings,
}), z.transform(({ missing_required, ...rest }) => ({ ...rest, missingRequired: missing_required })));
export type SemanticEvidenceBundle = z.infer<typeof evidenceBundle>;

const usage = z.pipe(z.object({
  input_tokens: z.optional(z.int().check(z.minimum(0))),
  output_tokens: z.optional(z.int().check(z.minimum(0))),
}), z.transform(({ input_tokens, output_tokens }) => ({ inputTokens: input_tokens, outputTokens: output_tokens })));

export const SemanticObservationSchema = z.pipe(z.object({
  observation_id: z.optional(z.string()),
  status: z.enum(["completed", "insufficient_evidence", "ambiguous", "provider_unavailable"]),
  answers: z.optional(recordSchema(SemanticAnswerSchema)),
  rule_outcomes: z.optional(recordSchema(CheckOutcomeSchema)),
  evidence: evidenceBundle,
  definition_fingerprint: z.string(), question_fingerprint: z.string(), evidence_fingerprint: z.string(),
  check_release: z.string(), latency_ms: z.optional(z.nullable(z.number().check(z.minimum(0)))),
  usage: z.optional(z.nullable(usage)),
}), z.transform(({
  observation_id, rule_outcomes, definition_fingerprint, question_fingerprint,
  evidence_fingerprint, check_release, latency_ms, ...rest
}) => ({
  ...rest, observationId: observation_id, ruleOutcomes: rule_outcomes,
  definitionFingerprint: definition_fingerprint, questionFingerprint: question_fingerprint,
  evidenceFingerprint: evidence_fingerprint, checkRelease: check_release, latencyMs: latency_ms,
})));
export type SemanticObservation = z.infer<typeof SemanticObservationSchema>;

const recoveryEvidence = z.pipe(z.object({
  binding: z.string(), path: z.string(), required: z.optional(z.boolean()), unresolved: z.optional(z.boolean()),
  status: evidenceStatus, origin: evidenceOrigin, source_id: optionalText, source_version: optionalText,
  fingerprint: z.string(),
}), z.transform(({ source_id, source_version, ...rest }) => ({
  ...rest, sourceId: source_id, sourceVersion: source_version,
})));
export type RecoveryEvidenceReference = z.infer<typeof recoveryEvidence>;

export interface RecoveryFeedback {
  ruleId: number; checkId: string; outcome: "matched" | "unresolved" | "error";
  reason: "criterion_triggered" | "missing_evidence" | "stale_evidence" | "uncertain_judgment" |
    "insufficient_origin" | "truncated_evidence" | "check_error";
  conditionIds?: string[]; conditionPath?: string | null;
  conditionOutcome?: "matched" | "not_matched" | "unresolved" | "error" | null; negated?: boolean;
  observationId?: string | null; definitionFingerprint?: string | null; evidenceFingerprint?: string | null;
  evidence?: RecoveryEvidenceReference[];
}
export const RecoveryFeedbackSchema: z.ZodMiniType<RecoveryFeedback, unknown> = z.pipe(z.object({
  rule_id: z.int(), check_id: z.string(),
  outcome: z.enum(["matched", "unresolved", "error"]),
  reason: z.enum([
    "criterion_triggered", "missing_evidence", "stale_evidence", "uncertain_judgment",
    "insufficient_origin", "truncated_evidence", "check_error",
  ]),
  condition_ids: optionalStrings, condition_path: optionalText,
  condition_outcome: z.optional(z.nullable(z.enum(["matched", "not_matched", "unresolved", "error"]))),
  negated: z.optional(z.boolean()), observation_id: optionalText,
  definition_fingerprint: optionalText, evidence_fingerprint: optionalText,
  evidence: z.optional(z.array(recoveryEvidence)),
}), z.transform(({
  rule_id, check_id, condition_ids, condition_path, condition_outcome,
  observation_id, definition_fingerprint, evidence_fingerprint, ...rest
}) => ({
  ...rest, ruleId: rule_id, checkId: check_id,
  conditionIds: condition_ids, conditionPath: condition_path, conditionOutcome: condition_outcome,
  observationId: observation_id, definitionFingerprint: definition_fingerprint, evidenceFingerprint: evidence_fingerprint,
})));
