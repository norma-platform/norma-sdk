import norma, { NormaClient, type CreateOperation } from "norma-sdk";
const proposal: CreateOperation = { operationKey: "stable-key", step: { type: "tool", name: "write", input: { value: 1 } } };

const client = new NormaClient();

const result = client.operations.create(proposal);
result.then((receipt) => { receipt.request.step.input satisfies Record<string, unknown>; });
void norma;

import { semanticDefinition,
  type SemanticStatementAnswer, type SemanticScoreAnswer } from "norma-sdk";
const statement: SemanticStatementAnswer = { type: "statement", support_score: 0.8 };
const rubric: SemanticScoreAnswer = { type: "score", score: 0.75, confidence: 0.8,
  legend: { "0": "Low", "1": "High" }, probabilities: { "0": 0.25, "1": 0.75 } };
const definition = semanticDefinition({ definition_id: "typed-consumer", check_release: "test",
  bindings: [{ name: "input", path: "input" }], questions: [
    { id: "supported", type: "statement", instructions: "Assess support", criteria: { true: "Supported", false: "Unsupported" } },
    { id: "quality", type: "score", instructions: "Assess quality", criteria: ["Low", "High"] }],
  decision: { on_unresolved: "observe", rules: [
    { type: "statement", question_id: "supported", threshold: 0.8 },
    { type: "score", question_id: "quality", min_score: 0.5 }] },
  examples: [{ state: {}, expected_answers: { supported: statement, quality: rubric } }] });
const view = definition;
for (const answer of Object.values(view.examples[0].expected_answers)) {
  if (answer.type === "statement") answer.support_score satisfies number;
  if (answer.type === "score") answer.score satisfies number;
}

const currentClient = new NormaClient();
currentClient.init({agentName:"app",runtimeUrl:"https://qualification.invalid",agentKey:"synthetic"});

import { checkpoint, RuleViolationError, RuleSteerError, CheckError, NormaAPIError,
  type StepSchema, type RuleDefinition, type Rule, type CheckRequest, type DeliveryReceipt } from "norma-sdk";
const registeredStep: StepSchema = { name: "lookup", type: "tool", inputSchema: { type: "object" } };
currentClient.agents.register({ agent: { agentName: "migration" }, steps: [registeredStep] });
const check: CheckRequest = { stage: "pre", step: { name: "lookup", type: "tool", input: {} } };
currentClient.checks.check(check).then((decision) => {
  decision.allowed satisfies boolean;
  decision.checks[0]?.ruleName satisfies string | undefined;
});
checkpoint(async (value: string) => value, { client: currentClient });
checkpoint(async (value: string, quantity: number) => ({ value, quantity }), {
  client: currentClient,
  state: async (value, quantity) => ({ entity: value, inventory: { available: quantity } }),
});
checkpoint(async () => null, { client: currentClient, state: { remaining: 3 } });
// @ts-expect-error State must be a JSON object, not an array.
checkpoint(async () => null, { client: currentClient, state: [] });
// @ts-expect-error Callback arguments follow the wrapped function.
checkpoint(async (value: string) => value, { client: currentClient, state: (value: number) => ({ value }) });
currentClient.flushObservability().then((delivery: DeliveryReceipt) => {
  delivery.persisted satisfies number | null;
});
void [RuleViolationError, RuleSteerError, CheckError, NormaAPIError];
// The installed declaration graph rejects removed APIs and local authoring.
// @ts-expect-error Raw check is internal.
currentClient.check;
currentClient.ruleBindings;
currentClient.agents.listRules;
const definitionType: RuleDefinition = {execution: "server", action: {decision: "deny"}, condition: {selector: {path: "input"}, evaluator: {name: "json", config: {}}}};
const ruleStatus: Rule["status"] = "unsupported";
// @ts-expect-error Generic rule templates are not part of the public Rule contract.
const templatedDefinition: RuleDefinition = { ...definitionType, template: {} };
// @ts-expect-error Generic template parameter values are not part of RuleDefinition.
const templateValuesDefinition: RuleDefinition = { ...definitionType, templateValues: {} };
// @ts-expect-error Shared parameter binding metadata is not part of RuleDefinition.
const parameterBoundDefinition: RuleDefinition = { ...definitionType, parameters: {} };
void [ruleStatus, templatedDefinition, templateValuesDefinition, parameterBoundDefinition];
// @ts-expect-error Hosted rule authoring cannot select local execution.
definitionType.execution = "sdk";
currentClient.checks.check(check).then(decision => {
  // @ts-expect-error Raw wire results cannot escape public Decisions.
  decision.raw;
  decision.checks[0].ruleId satisfies number;
});

// @ts-expect-error Vocabulary selectors belonged to removed wire conversion APIs.
import type { SemanticAuthoringVocabulary } from "norma-sdk";
