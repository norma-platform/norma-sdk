// Modified for Norma.
/** Private protocol adapters. Never export these implementation functions from the package. */
import {jsonObjectSchema} from "../json-contract";
import type { NormaConfig } from "../client";
import type { NormaSDK } from "../generated/sdk/sdk";
import type { RuleMatch } from "../generated/models/rule-match";
import type { CheckResponse } from "../generated/models/check-response";
import type { RuleDefinitionOutput } from "../generated/models/rule-definition-output";
import type { Decision, CheckResult, RuleDecision, ChecksApi, RuleDefinition, Rule, RuleBinding,
  AgentsApi, RuleBindingsApi, EvaluatorsApi, ObservabilityApi, RuleCondition } from "../public-api";
export function decisionFromWire(response: CheckResponse): Decision {
  const convert = (match: RuleMatch, status: CheckResult["status"]): CheckResult => {
    const action = (value: unknown): RuleDecision => {
      if (value !== "deny" && value !== "steer" && value !== "observe") throw new TypeError("Unsupported rule action in check response.");
      return value;
    };
    const result = match.result;
    return { ruleId: match.ruleId, ruleName: match.ruleName, checkId: match.checkId,
      status, mode: match.mode, action: action(match.action), effectiveAction: match.effectiveAction == null ? match.effectiveAction : action(match.effectiveAction),
      steeringContext: match.steeringContext == null ? match.steeringContext : {message: match.steeringContext.message},
      result: {matched: result.matched, confidence: result.confidence, outcome: result.outcome,
        error: result.error, message: result.message, metadata: result.metadata,
        semanticObservation: result.semanticObservation, failedConditionIds: result.failedConditionIds,
        unresolvedEvidenceIds: result.unresolvedEvidenceIds,
        unresolvedAction: result.unresolvedAction == null ? result.unresolvedAction : action(result.unresolvedAction)} };

  };
  const checks = [
    ...(response.matches ?? []).map(match => convert(match, "matched")),
    ...(response.nonMatches ?? []).map(match => convert(match, "not_matched")),
    ...(response.unresolved ?? []).map(match => convert(match, "unresolved")),
    ...(response.errors ?? []).map(match => convert(match, "error")),
  ];
  const blocking = checks.some(check => check.mode !== "observe" && (check.status === "error" || check.result.error != null ||
    (["matched", "unresolved"].includes(check.status) && ["deny", "steer"].includes(check.effectiveAction ?? check.action))));
  return { allowed: response.allowed && !blocking, reason: response.reason ?? null, checks, recoveryFeedback: response.recoveryFeedback };
}
export function createChecks(check: NormaSDK["checks"], config: NormaConfig): ChecksApi {
  return { async check(request, options) {
    return decisionFromWire(await check.check({ targetType: config.targetType, targetId: config.targetId,
      ...request, agentName: config.agentName }, options));
  } };
}
export function ruleDefinitionFromWire(definition: RuleDefinitionOutput): RuleDefinition | null {
  const decision = definition.action.decision;
  if (definition.execution !== "server" || !["deny", "steer", "observe"].includes(decision)) return null;
  if (definition.scope?.stages?.some(stage => stage !== "pre" && stage !== "post")) return null;
  const condition = (node: RuleDefinitionOutput["condition"]): RuleCondition => ({
    and: node.and?.map(condition), or: node.or?.map(condition), not: node.not == null ? node.not : condition(node.not),
    selector: node.selector == null ? node.selector : {path: node.selector.path},
    evaluator: node.evaluator == null ? node.evaluator : {name: node.evaluator.name,
      config: node.evaluator.config},
  });
  return {...(definition.parameters == null ? {} : {parameters: jsonObjectSchema.parse(definition.parameters)}),
    mode: definition.mode, execution: "server", enabled: definition.enabled, action: {decision: decision as RuleDecision,
    steeringContext: definition.action.steeringContext}, condition: condition(definition.condition),
    description: definition.description, scope: definition.scope == null ? undefined : {
      stepTypes: definition.scope.stepTypes, stepNames: definition.scope.stepNames,
      stepNameRegex: definition.scope.stepNameRegex,
      stages: definition.scope.stages?.map(stage => stage as "pre" | "post") ?? definition.scope.stages as null | undefined,
    }, tags: definition.tags,
    template: definition.template == null ? definition.template : jsonObjectSchema.parse(definition.template), templateValues: definition.templateValues};
}

export function publicApis(sdk: NormaSDK): {agents: AgentsApi; ruleBindings: RuleBindingsApi; evaluators: EvaluatorsApi; observability: ObservabilityApi} {
  const rule = (value: {id: number; name: string; rule: unknown}): Rule => {
    const definition = value.rule as RuleDefinitionOutput;
    const unrendered = !("execution" in definition);
    const publicDefinition = unrendered ? null : ruleDefinitionFromWire(definition);
    return { id: value.id, name: value.name, definition: publicDefinition,
      status: unrendered ? "unrendered" : publicDefinition !== null ? "hosted" : "unsupported" };
  };
  const binding = (value: Awaited<ReturnType<NormaSDK["ruleBindings"]["get"]>>): RuleBinding => {
    const { ruleId, namespaceKey: _namespace, ...rest } = value; void _namespace;
    return {...rest, ruleId: ruleId};
  };
  return {
    agents: {
      async list(request, options) { const response = await sdk.agents.list(request, options);
        return { ...response, agents: response.agents.map(({activeRulesCount, ...rest}) => ({...rest, activeRulesCount: activeRulesCount})) }; },
      async register(request, options) { const {rules, overwriteChanges, ...rest} = await sdk.agents.register(request, options);
        return { ...rest, rules: rules?.map(rule), overwriteChanges: overwriteChanges == null ? overwriteChanges : {
          ...overwriteChanges, evaluatorRemovals: overwriteChanges.evaluatorRemovals?.map(({ruleIds, ruleNames, referencedByActiveRules, ...item}) => ({
            ...item, ruleIds: ruleIds, ruleNames: ruleNames, referencedByActiveRules: referencedByActiveRules })) } }; },
      get: (request, options) => sdk.agents.get(request, options), update: (request, options) => sdk.agents.update(request, options),
      async listRules(request, options) { const result = await sdk.agents.listRules(request, options); return {rules: result.rules.map(rule)}; },
      listEvaluators: (request, options) => sdk.agents.listEvaluators(request, options),
      getEvaluator: (request, options) => sdk.agents.getEvaluator(request, options),
    },
    ruleBindings: { async list(request = {}, options) { const {ruleId, ...rest} = request;
      const response = await sdk.ruleBindings.list({...rest, ruleId: ruleId}, options);
      return {...response, bindings: response.bindings?.map(binding)}; },
      async get(request, options) { return binding(await sdk.ruleBindings.get(request, options)); } },
    evaluators: {list: options => sdk.evaluators.list(options)},
    observability: {ingestEvents: (request, options) => sdk.observability.ingestEvents({events: request.events.map(({ruleId, ruleName, checkId, ...rest}) => ({
      ...rest, ruleId: ruleId, ruleName: ruleName, checkId: checkId }))}, options)},
  };
}
