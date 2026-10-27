// Modified for Norma.
/** Public, versioned Console controls receipts and frozen execution evidence. */
import * as z from "zod/v4-mini";
import { remap as remap$ } from "../lib/primitives.js";

export type JsonValue = null | boolean | number | string | readonly JsonValue[] | Readonly<{ [key: string]: JsonValue }>;
export type ControlValueSource = "default" | "agent_override";
export type ControlReceiptReference = {
  readonly key: string; readonly sourcePointer: string; readonly destinationPointer: string; readonly evaluatorPointer: string;
  readonly slot: Readonly<{ [key: string]: JsonValue }>;
};
export type ControlReceipt = {
  readonly contract: "rule-controls-v1"; readonly capabilitiesFingerprint: string; readonly declarationFingerprint: string;
  readonly defaults: Readonly<{ [key: string]: JsonValue }>; readonly effectiveValues: Readonly<{ [key: string]: JsonValue }>;
  readonly valuesFingerprint: string; readonly valueSources: Readonly<{ [key: string]: ControlValueSource }>;
  readonly references: readonly ControlReceiptReference[]; readonly overriddenKeys: readonly string[]; readonly agentGeneration: string;
  readonly agentName: string; readonly ruleId: number; readonly ruleVersion: number; readonly overrideRevision: number;
  readonly effectiveFingerprint: string;
};
export type NumericComparisonEvidence = {
  readonly fieldPath: string; readonly operator: "min" | "max"; readonly observed: number | null; readonly expected: number;
  readonly outcome: "passed" | "failed" | "not_evaluated"; readonly observedState?: "redacted" | null;
  readonly reason?: "field_missing" | "not_numeric" | "not_finite" | "prerequisite_failed" | null;
};
export type ControlReferenceEvidence = {
  readonly key: string; readonly destinationPointer: string; readonly conditionId?: string | null; readonly configuredDefault: JsonValue;
  readonly effectiveValue: JsonValue; readonly valueSource: ControlValueSource;
  readonly disposition: "configured" | "evaluated" | "skipped" | "unavailable";
  readonly unavailableReason?: "trace_missing" | "cancelled_without_trace" | "evaluator_error" |
    "comparison_not_evaluated" | "comparison_missing" | null;
  readonly numericComparisons: readonly NumericComparisonEvidence[]; readonly numericComparisonsTruncated?: boolean | null;
};
export type ControlEvidenceSnapshot = {
  readonly schemaVersion: 1; readonly completeness: "complete" | "incomplete" | "unavailable"; readonly agentName: string;
  readonly agentGeneration: string; readonly ruleId: number; readonly ruleVersion?: number | null; readonly definitionFingerprint: string;
  readonly concreteDefinitionFingerprint?: string | null; readonly effectiveDefinition?: Readonly<{ [key: string]: JsonValue }> | null;
  readonly receipt?: ControlReceipt | null; readonly references: readonly ControlReferenceEvidence[];
  readonly numericComparisonsTruncated?: boolean | null;
};

const json = z.any();
function freezeDeep<T>(value: T): T {
  if (value !== null && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value as Record<string, unknown>)) freezeDeep(child);
    Object.freeze(value);
  }
  return value;
}
function canonicalJson(value: unknown): string {
  if (value === null || typeof value === "string" || typeof value === "boolean") {
    return JSON.stringify(value);
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) throw new TypeError("control evidence values must be finite JSON");
    return Object.is(value, -0) ? "-0" : JSON.stringify(value);
  }
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map(key => `${JSON.stringify(key)}:${canonicalJson(record[key])}`).join(",")}}`;
  }
  throw new TypeError("control evidence values must be JSON");
}
const receiptReferenceSchema = z.pipe(z.object({
  key: z.string(), source_pointer: z.string(), destination_pointer: z.string(),
  evaluator_pointer: z.string(), slot: z.record(z.string(), json),
}), z.transform((v) => freezeDeep(remap$(v, { source_pointer: "sourcePointer", destination_pointer: "destinationPointer", evaluator_pointer: "evaluatorPointer" }))));
export const ControlReceipt$inboundSchema = z.pipe(z.object({
  contract: z.literal("rule-controls-v1"), capabilities_fingerprint: z.string(), declaration_fingerprint: z.string(),
  defaults: z.record(z.string(), json), effective_values: z.record(z.string(), json), values_fingerprint: z.string(),
  value_sources: z.record(z.string(), z.enum(["default", "agent_override"])),
  references: z.array(receiptReferenceSchema), overridden_keys: z.array(z.string()),
  agent_generation: z.string(), agent_name: z.string(), rule_id: z.int().check(z.gte(1)),
  rule_version: z.int().check(z.gte(1)), override_revision: z.int().check(z.gte(0)),
  effective_fingerprint: z.string(),
}), z.transform((v) => freezeDeep(remap$(v, {
  capabilities_fingerprint: "capabilitiesFingerprint", declaration_fingerprint: "declarationFingerprint",
  effective_values: "effectiveValues", values_fingerprint: "valuesFingerprint", value_sources: "valueSources",
  overridden_keys: "overriddenKeys", agent_generation: "agentGeneration", agent_name: "agentName",
  rule_id: "ruleId", rule_version: "ruleVersion", override_revision: "overrideRevision",
  effective_fingerprint: "effectiveFingerprint",
}))));

const comparisonSchema = z.pipe(z.object({
  field_path: z.string(), operator: z.enum(["min", "max"]), observed: z.nullable(z.number()), expected: z.number(),
  outcome: z.enum(["passed", "failed", "not_evaluated"]), observed_state: z.optional(z.nullable(z.literal("redacted"))),
  reason: z.optional(z.nullable(z.enum(["field_missing", "not_numeric", "not_finite", "prerequisite_failed"]))),
}), z.transform((v) => {
  if (!Number.isFinite(v.expected) || (v.observed !== null && !Number.isFinite(v.observed))) {
    throw new TypeError("numeric comparison evidence must be finite");
  }
  if (v.observed_state === "redacted") {
    if (v.observed !== null || v.outcome === "not_evaluated" || v.reason != null) throw new TypeError("invalid redacted comparison evidence");
  } else if (v.outcome === "not_evaluated") {
    if (v.observed !== null || v.reason == null) throw new TypeError("invalid not-evaluated comparison evidence");
  } else if (v.observed === null || v.reason != null) {
    throw new TypeError("invalid evaluated comparison evidence");
  }
  return freezeDeep(remap$(v, { field_path: "fieldPath", observed_state: "observedState" }));
}));
const referenceEvidenceSchema = z.pipe(z.object({
  key: z.string(), destination_pointer: z.string(), condition_id: z.optional(z.nullable(z.string())),
  configured_default: json, effective_value: json, value_source: z.enum(["default", "agent_override"]),
  disposition: z.enum(["configured", "evaluated", "skipped", "unavailable"]),
  unavailable_reason: z.optional(z.nullable(z.enum(["trace_missing", "cancelled_without_trace", "evaluator_error", "comparison_not_evaluated", "comparison_missing"]))),
  numeric_comparisons: z.array(comparisonSchema), numeric_comparisons_truncated: z.optional(z.nullable(z.boolean())),
}), z.transform((v) => freezeDeep(remap$(v, {
  destination_pointer: "destinationPointer", condition_id: "conditionId", configured_default: "configuredDefault",
  effective_value: "effectiveValue", value_source: "valueSource", unavailable_reason: "unavailableReason",
  numeric_comparisons: "numericComparisons", numeric_comparisons_truncated: "numericComparisonsTruncated",
}))));

export const ControlEvidenceSnapshot$inboundSchema = z.pipe(z.object({
  schema_version: z.literal(1), completeness: z.enum(["complete", "incomplete", "unavailable"]),
  agent_name: z.string(), agent_generation: z.string(), rule_id: z.int().check(z.gte(1)),
  rule_version: z.optional(z.nullable(z.int().check(z.gte(1)))),
  definition_fingerprint: z.string(), concrete_definition_fingerprint: z.optional(z.nullable(z.string())),
  effective_definition: z.optional(z.nullable(z.record(z.string(), json))),
  receipt: z.optional(z.nullable(ControlReceipt$inboundSchema)), references: z.array(referenceEvidenceSchema),
  numeric_comparisons_truncated: z.optional(z.nullable(z.boolean())),
}), z.transform((v) => {
  if (v.references.length > 256 || v.references.reduce((sum, item) => sum + item.numericComparisons.length, 0) > 64) {
    throw new TypeError("control evidence exceeds its bounded reference or comparison limit");
  }
  if (v.completeness === "unavailable" && v.references.length > 0) throw new TypeError("unavailable evidence cannot assert reference dispositions");
  if (v.receipt && (v.receipt.agentName !== v.agent_name || v.receipt.agentGeneration !== v.agent_generation
    || v.receipt.ruleId !== v.rule_id || v.receipt.ruleVersion !== v.rule_version
    || v.definition_fingerprint !== v.receipt.effectiveFingerprint)) {
    throw new TypeError("control evidence identity does not match its receipt");
  }
  if (v.receipt) {
    const identity = (item: { key: string; destinationPointer: string }) =>
      JSON.stringify([item.key, item.destinationPointer]);
    const receiptRefs = new Map<string, (typeof v.receipt.references)[number]>();
    for (const item of v.receipt.references) {
      const key = identity(item);
      if (receiptRefs.has(key)) throw new TypeError("control receipt references must be unique");
      receiptRefs.set(key, item);
    }
    const seen = new Set<string>();
    for (const item of v.references) {
      const key = identity(item);
      if (seen.has(key)) throw new TypeError("control evidence references must be unique");
      seen.add(key);
      const receiptRef = receiptRefs.get(key);
      if (!receiptRef) throw new TypeError("control evidence references must be present in its receipt");
      const valueKey = receiptRef.key;
      if (!Object.hasOwn(v.receipt.defaults, valueKey)
        || !Object.hasOwn(v.receipt.effectiveValues, valueKey)
        || !Object.hasOwn(v.receipt.valueSources, valueKey)) {
        throw new TypeError("control receipt is missing a referenced value");
      }
      if (canonicalJson(item.configuredDefault) !== canonicalJson(v.receipt.defaults[valueKey])
        || canonicalJson(item.effectiveValue) !== canonicalJson(v.receipt.effectiveValues[valueKey])
        || item.valueSource !== v.receipt.valueSources[valueKey]) {
        throw new TypeError("control evidence values must match its receipt");
      }
    }
    if (v.completeness === "complete" && seen.size !== receiptRefs.size) {
      throw new TypeError("complete control evidence references do not match the receipt");
    }
  } else if (v.references.length > 0) {
    throw new TypeError("control evidence references require a receipt");
  }
  if (v.completeness === "complete") {
    if (v.rule_version == null || !v.receipt || !v.effective_definition || !v.concrete_definition_fingerprint) {
      throw new TypeError("complete control evidence requires frozen definition fingerprints and receipt");
    }
  }
  return freezeDeep(remap$(v, {
  schema_version: "schemaVersion", agent_name: "agentName", agent_generation: "agentGeneration",
  rule_id: "ruleId", rule_version: "ruleVersion", definition_fingerprint: "definitionFingerprint",
  concrete_definition_fingerprint: "concreteDefinitionFingerprint", effective_definition: "effectiveDefinition",
  numeric_comparisons_truncated: "numericComparisonsTruncated",
  }));
}));
