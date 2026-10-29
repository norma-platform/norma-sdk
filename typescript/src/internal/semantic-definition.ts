// Modified for Norma.
/** Typed Norma authoring for the native semantic v1 JSON contract. */
import * as z from "zod/v4-mini";

import { copyCheckpointArguments } from "../checkpoint-arguments";
import type { JsonObject } from "../types";
import { inferSemanticDecisionTypes } from "./semantic-authoring";

const text = (minimum: number, maximum: number) => z.string().check(
  z.refine((value) => Array.from(value).length >= minimum && Array.from(value).length <= maximum),
);
const identifier = text(1, 128).check(z.regex(/^[a-zA-Z0-9_-]+(?![\s\S])/));
const instructions = text(1, 16000);
const probability = z.number().check(z.minimum(0), z.maximum(1));
const optional = <T extends z.ZodMiniType>(schema: T) => z._default(z.nullable(schema), null);
const list = <T extends z.ZodMiniType>(schema: T, minimum: number, maximum: number) =>
  z.array(schema).check(z.minLength(minimum), z.maxLength(maximum));
const isObject = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
// z.record/z.json omit __proto__ keys. Author-owned JSON keys must remain exact;
// copyJson already validates and detaches every nested value before these schemas run.
const record = <T extends z.ZodMiniType>(key: z.ZodMiniString, value: T) =>
  z.custom<Record<string, z.output<T>>>((input) => isObject(input)
    && Object.entries(input).every(([name, item]) => key.safeParse(name).success && value.safeParse(item).success));
const path = text(1, 512).check(z.refine((value) => {
  const parts = value.split(".");
  return ["*", "input", "output", "context", "name", "type", "tools", "ground_truth"].includes(parts[0])
    && parts.every((part) => part.length > 0 && !part.startsWith("__"))
    && (parts[0] !== "*" || parts.length === 1);
}, "Evidence path must reference Step data without invalid components"));
const binding = z.strictObject({
  name: identifier.check(z.refine((value) => !value.startsWith("_norma_"), "Reserved evidence name")),
  path,
  required: z._default(z.boolean(), true),
  required_origin: z._default(z.enum(["caller_asserted", "runtime_observed", "source_verified"]), "caller_asserted"),
  source_id: optional(text(0, 255)),
  source_version: optional(text(0, 255)),
  observed_at_path: optional(path),
  max_age_seconds: optional(z.number().check(z.gt(0))),
  max_chars: optional(z.int().check(z.minimum(1), z.maximum(100000))),
}).check(z.refine((value) => value.max_age_seconds === null || value.observed_at_path !== null,
  "max_age_seconds requires observed_at_path"));

const questionFields = { id: identifier, instructions };
// Statement criteria retain the compatible true/false keys for the two described poles.
// A support score is a graded judgment, not objective truth, correctness confidence or authorization.
const statementQuestion = z.strictObject({ ...questionFields, type: z.literal("statement"),
  criteria: z.strictObject({ true: instructions, false: instructions }) });


const choiceQuestion = z.strictObject({ ...questionFields, type: z.literal("choice"),
  criteria: record(identifier, instructions).check(z.refine((value) => {
    const count = Object.keys(value).length;
    return count >= 2 && count <= 255;
  })) });
const scoreQuestion = z.strictObject({ ...questionFields, type: z.literal("score"),
  criteria: list(instructions, 2, 10) });

const publicQuestion = z.discriminatedUnion("type", [statementQuestion, choiceQuestion, scoreQuestion]);
const runtimeQuestion = z.discriminatedUnion("type", [statementQuestion, choiceQuestion, scoreQuestion]);

const truthRuleFields = {
  question_id: identifier,
  match_when: z._default(z.enum(["at_least", "at_most"]), "at_least"),
  threshold: probability,
  ambiguous_band: optional(z.tuple([probability, probability]).check(
    z.refine(([low, high]) => low <= high, "Ambiguous band must be ordered"),
  )),
};
const statementRule = z.strictObject({ type: z.literal("statement"), ...truthRuleFields });


const choiceRule = z.strictObject({
  type: z.literal("choice"), question_id: identifier,
  match_choices: z.array(identifier).check(z.minLength(1)),
  unresolved_choices: z._default(z.array(identifier), []),
  minimum_probability: optional(probability),
});
const scoreRuleFields = {
  question_id: identifier,
  min_score: optional(z.number().check(z.minimum(0))),
  max_score: optional(z.number().check(z.minimum(0))),
  minimum_confidence: optional(probability),
};
const scoreRule = z.strictObject({ type: z.literal("score"), ...scoreRuleFields }).check(z.refine(({ min_score, max_score }) => (min_score !== null || max_score !== null)
  && (min_score === null || max_score === null || min_score <= max_score), "Score interval requires ordered bounds"));

const publicRule = z.discriminatedUnion("type", [statementRule, choiceRule, scoreRule]);
const runtimeRule = z.discriminatedUnion("type", [statementRule, choiceRule, scoreRule]);

const statementAnswer = z.strictObject({ type: z.literal("statement"), support_score: probability });


const choiceAnswer = z.strictObject({
  type: z.literal("choice"), choice: z.string(), confidence: probability,
  probabilities: record(z.string(), probability),
});
const scoreAnswerFields = {
  score: z.number().check(z.minimum(0)), confidence: probability,
  legend: record(z.string(), z.string()), probabilities: record(z.string(), probability),
};
const scoreAnswer = z.strictObject({ type: z.literal("score"), ...scoreAnswerFields });

const publicAnswer = z.discriminatedUnion("type", [statementAnswer, choiceAnswer, scoreAnswer]);
const runtimeAnswer = z.discriminatedUnion("type", [statementAnswer, choiceAnswer, scoreAnswer]);

const decisionFields = {
  operator: z._default(z.enum(["all", "any"]), "all"),
  on_unresolved: z.enum(["deny", "steer", "observe"]),
};
const definitionFields = {
  schema_version: z._default(z.literal("1"), "1"),
  definition_id: identifier,
  definition_version: z._default(z.int().check(z.minimum(1)), 1),
  check_release: text(1, 128),
  bindings: list(binding, 1, 32),
};
const exampleFields = { state: z.custom<JsonObject>(isObject), note: optional(text(0, 4000)) };
const publicDefinition = z.strictObject({
  ...definitionFields,
  // Omitted authoring metadata is assigned and pinned by the hosted runtime.
  definition_id: z.optional(identifier),
  check_release: z.optional(text(1, 128)),
  questions: list(publicQuestion, 1, 32),
  decision: z.strictObject({ ...decisionFields, rules: list(publicRule, 1, 32) }),
  examples: z._default(list(z.strictObject({ ...exampleFields,
    expected_answers: record(z.string(), publicAnswer) }), 0, 32), []),
});
const runtimeDefinition = z.strictObject({
  ...definitionFields,
  questions: list(runtimeQuestion, 1, 32),
  decision: z.strictObject({ ...decisionFields, rules: list(runtimeRule, 1, 32) }),
  examples: z._default(list(z.strictObject({ ...exampleFields,
    expected_answers: record(z.string(), runtimeAnswer) }), 0, 32), []),
});

/** Statement results use a support score, a graded judgment from zero to one. */
export type SemanticStatementQuestion = z.infer<typeof statementQuestion>;
/** Threshold and ambiguous_band are independent authored policy settings. */
export type SemanticStatementRule = z.infer<typeof statementRule>;
/** Support score is a graded judgment, not objective truth, calibrated correctness confidence or authorization. */
export type SemanticStatementAnswer = z.infer<typeof statementAnswer>;
/** Ordered criteria use zero-based rubric indices. */
export type SemanticScoreQuestion = z.infer<typeof scoreQuestion>;
export type SemanticScoreRule = z.infer<typeof scoreRule>;
export type SemanticScoreAnswer = z.infer<typeof scoreAnswer>;
export type NormaSemanticAnswer = z.infer<typeof publicAnswer>;
export type NormaSemanticQuestion = z.infer<typeof publicQuestion>;
export type NormaSemanticRule = z.infer<typeof publicRule>;
type AuthoredDecisionRule<T extends { type: string }> = T extends unknown
  ? Omit<T, "type"> & { type?: T["type"] }
  : never;
type PublicDefinitionInput = z.input<typeof publicDefinition>;
export type NormaSemanticDefinitionInput = Omit<PublicDefinitionInput, "decision"> & {
  decision: Omit<PublicDefinitionInput["decision"], "rules"> & {
    rules: AuthoredDecisionRule<z.input<typeof publicRule>>[];
  };
};
export type NormaSemanticDefinition = z.infer<typeof publicDefinition>;
/** Validated authored config; omitted identity and release are resolved by the server. */
export type SemanticDefinitionConfig = NormaSemanticDefinition;

function copyJson(value: unknown): unknown {
  try { return copyCheckpointArguments([value], false)[0]; }
  catch { throw new TypeError("Semantic authoring requires finite, plain JSON data without accessors or cycles"); }
}

function requireValid(condition: boolean, message: string): asserts condition {
  if (!condition) throw new TypeError(message);
}

function sameKeys(left: object, right: object): boolean {
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length && keys.every((key) => Object.hasOwn(right, key));
}

function coherentDefinition<T extends SemanticDefinitionConfig>(definition: T): T {
  const questions = new Map(definition.questions.map((question) => [question.id, question]));
  requireValid(questions.size === definition.questions.length, "Semantic question IDs must be unique");
  for (const key of ["name", "path"] as const) {
    requireValid(new Set(definition.bindings.map((item) => item[key])).size === definition.bindings.length,
      `Evidence binding ${key}s must be unique`);
  }
  requireValid(new Set(definition.decision.rules.map((rule) => rule.question_id)).size === definition.decision.rules.length,
    "Each decision question must have one rule");
  for (const rule of definition.decision.rules) {
    const question = questions.get(rule.question_id);
    requireValid(question !== undefined && question.type === rule.type,
      "Decision rule must reference a question with the same type");
    if (rule.type === "choice" && question.type === "choice") {
      requireValid(rule.match_choices.every((choice) => !rule.unresolved_choices.includes(choice)),
        "Match and unresolved choices must be disjoint");
      requireValid([...rule.match_choices, ...rule.unresolved_choices].every((choice) => Object.hasOwn(question.criteria, choice)),
        "Choice rule references an unknown question candidate");
    }
    if (rule.type === "score" && question.type === "score") {
      requireValid([rule.min_score, rule.max_score].every((bound) => bound === null || bound <= question.criteria.length - 1),
        "Score rule exceeds its question rubric");
    }
  }
  for (const example of definition.examples) {
    for (const [id, answer] of Object.entries(example.expected_answers)) {
      const question = questions.get(id);
      requireValid(question !== undefined && question.type === answer.type,
        "Example answer must reference a question with the same type");
      if (answer.type === "statement") continue;
      const total = Object.values(answer.probabilities).reduce((sum, value) => sum + value, 0);
      requireValid(Math.abs(total - 1) <= 0.001, "Answer probabilities must sum to one");
      if (answer.type === "choice" && question.type === "choice") {
        requireValid(sameKeys(answer.probabilities, question.criteria), "Choice probabilities must cover exactly the question candidates");
        requireValid(Object.hasOwn(question.criteria, answer.choice), "Choice answer must select a known question candidate");
        requireValid(Object.values(answer.probabilities).every((value) => value <= answer.probabilities[answer.choice]),
          "Selected choice must have the highest probability");
      }
      if (answer.type === "score" && question.type === "score") {
        const legend = Object.fromEntries(question.criteria.map((criterion, index) => [String(index), criterion]));
        requireValid(sameKeys(answer.legend, legend) && sameKeys(answer.probabilities, legend)
          && Object.entries(legend).every(([key, value]) => answer.legend[key] === value),
        "Score answer must use the question's complete ordered rubric");
        const expected = Object.entries(answer.probabilities).reduce((sum, [index, value]) => sum + Number(index) * value, 0);
        requireValid(Math.abs(answer.score - expected) <= Math.max(0.01, 1e-9 * Math.max(answer.score, expected)),
          "Score answer must agree with its rubric probability distribution");
      }
    }
  }
  return definition;
}

/** Validate and detach a native statement, choice or score answer. */
export function toRuntimeSemanticAnswer(answer: NormaSemanticAnswer): SemanticAnswerView {
  return runtimeAnswer.parse(copyJson(answer));
}

export type SemanticAnswerView = z.infer<typeof runtimeAnswer>;

/** Return a detached native answer, suitable for Array.map as well as direct calls. */
export function toNormaSemanticAnswer(answer: unknown): NormaSemanticAnswer {
  return publicAnswer.parse(copyJson(answer));
}

/** Validate native authoring without translating protocol vocabulary. */
export function toRuntimeSemanticDefinition(config: NormaSemanticDefinitionInput): SemanticDefinitionConfig {
  const detached = copyJson(config);
  return coherentDefinition(publicDefinition.parse(
    isObject(detached) ? inferSemanticDecisionTypes(detached) : detached,
  ));
}

/** Validate native runtime definitions and preserve evidence and fingerprints exactly. */
export function fromRuntimeSemanticDefinition(config: unknown): NormaSemanticDefinition {
  return coherentDefinition(runtimeDefinition.parse(copyJson(config)));
}

/** Validate and detach authoring, adding shared model defaults. */
export function semanticDefinition(config: NormaSemanticDefinitionInput): NormaSemanticDefinition {
  return toRuntimeSemanticDefinition(config);
}
