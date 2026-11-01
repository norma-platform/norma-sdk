// Modified for Norma.
import { isMap, isScalar, isSeq, parseDocument } from "yaml";
import type { InstrumentationConfig } from "./instrumentation";

export type CoverageStatus = "covered" | "missing" | "unknown";
export interface RuleCoverageItem {
  rule_index: number;
  rule_path: string;
  checkpoint: string;
  stage: "pre" | "post";
  selector: string;
  status: CoverageStatus;
}
export interface RuleCoverageDiagnostic { code: string; path: string; message: string }
export interface RuleEntry { definition: Record<string, unknown>; scope: unknown; declarationBase: string; scopePath: string; agentName?: string }
interface SelectorDeclaration { path: string; selector: string; nullIsMissing?: boolean; semanticArray?: boolean; forceUnknown?: boolean }

const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
const UNKNOWN_VALUE = Symbol("unknown-shape");
const UNKNOWN_ARRAY = Symbol("unknown-array-shape");
const PARTIAL_OBJECT = Symbol("partial-object-shape");
const MISSING_VALUE = Symbol("missing-shape");
const MAX_RULE_GRAPH_DEPTH = 100;
const MAX_RULE_GRAPH_NODES = 100_000;
function compareText(left: string, right: string): number {
  const a = Array.from(left, value => value.codePointAt(0)!);
  const b = Array.from(right, value => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(a.length, b.length); index += 1) {
    if (a[index] !== b[index]) return a[index]! < b[index]! ? -1 : 1;
  }
  return a.length - b.length;
}
const safeParse = (source: string, format: "json" | "yaml"): unknown => {
  const document = parseDocument(source, { uniqueKeys: true, version: "1.2" });
  if (document.errors.length) throw new Error("syntax");
  if (format === "json") return JSON.parse(source) as unknown;
  return document.toJS({ maxAliasCount: 50 }) as unknown;
};

function nonStringFieldMapKeyPath(source: string): string | undefined {
  const document = parseDocument(source, { uniqueKeys: true, version: "1.2" });
  if (document.errors.length) return undefined;
  const visit = (node: unknown, path: string): string | undefined => {
    if (isMap(node)) {
      for (const pair of node.items) {
        const key = isScalar(pair.key) && typeof pair.key.value === "string" ? pair.key.value : undefined;
        const nextPath = `${path}/${key === undefined ? "<key>" : key.replace(/~/g, "~0").replace(/\//g, "~1")}`;
        if ((key === "field_types" || key === "field_constraints") && isMap(pair.value)) {
          for (const fieldPair of pair.value.items) {
            if (!isScalar(fieldPair.key) || typeof fieldPair.key.value !== "string") return `${nextPath}/<key>`;
          }
        }
        const found = visit(pair.value, nextPath);
        if (found) return found;
      }
    } else if (isSeq(node)) {
      for (const [index, child] of node.items.entries()) {
        const found = visit(child, `${path}/${index}`);
        if (found) return found;
      }
    }
    return undefined;
  };
  return visit(document.contents, "");
}

/** YAML aliases can create object cycles. Reject cycles and unreasonable graphs
 * before wrapper/condition traversal so malformed input always returns a bounded
 * schema diagnostic instead of hanging or overflowing the stack. */
function assertBoundedAcyclicGraph(root: unknown): void {
  const active = new WeakSet<object>();
  const complete = new WeakSet<object>();
  const pending: Array<{ value: unknown; depth: number; exit?: boolean }> = [{ value: root, depth: 0 }];
  let visited = 0;
  while (pending.length) {
    const current = pending.pop()!;
    if (!current.value || typeof current.value !== "object") continue;
    if (current.exit) { active.delete(current.value); complete.add(current.value); continue; }
    if (active.has(current.value)) throw new Error("rules_schema");
    if (complete.has(current.value)) continue;
    if (current.depth > MAX_RULE_GRAPH_DEPTH || ++visited > MAX_RULE_GRAPH_NODES) throw new Error("rules_schema");
    active.add(current.value);
    pending.push({ value: current.value, depth: current.depth, exit: true });
    const children = Array.isArray(current.value) ? current.value : Object.values(current.value);
    for (const child of children) pending.push({ value: child, depth: current.depth + 1 });
  }
}

function fail(code: string, path = ""): never {
  const error = new Error(code) as Error & { code: string; path: string };
  error.code = code;
  error.path = path;
  throw error;
}

function joinPointer(base: string, suffix: string): string { return `${base}${suffix}` || ""; }

function unpackRule(value: unknown, path: string, base: string, agentName?: string): RuleEntry {
  if (!record(value)) fail("rules_schema", path);
  let definition: unknown = value;
  let declarationBase = base;
  let scope: unknown;
  let scopePath = joinPointer(base, "/scope");
  let cursor = value;
  while (record(cursor) && (record(cursor.definition) || record(cursor.rule))) {
    const key = record(cursor.definition) ? "definition" : "rule";
    declarationBase = joinPointer(declarationBase, `/${key}`);
    cursor = cursor[key] as Record<string, unknown>;
    definition = cursor;
  }
  if (!record(definition)) fail("rules_schema", path);
  if (Object.hasOwn(definition, "scope")) { scope = definition.scope; scopePath = joinPointer(declarationBase, "/scope"); }
  return { definition, scope, declarationBase, scopePath, ...(agentName === undefined ? {} : { agentName }) };
}

/** Parse the supported local Rule export and draft shapes without evaluating Rules. */
export function parseLocalRules(source: string, format: "json" | "yaml", configAgentName: string): RuleEntry[] {
  let value: unknown;
  if (format === "yaml") {
    let nonStringKey: string | undefined;
    try { nonStringKey = nonStringFieldMapKeyPath(source); } catch { fail("rules_schema"); }
    if (nonStringKey) fail("rules_schema", nonStringKey);
  }
  try { value = safeParse(source, format); assertBoundedAcyclicGraph(value); } catch { fail("rules_schema"); }
  let agentName: string | undefined;
  let pointerRoot = "";
  if (record(value) && typeof value.jsonText === "string") {
    agentName = typeof value.agentName === "string" ? value.agentName : undefined;
    if (agentName && agentName !== configAgentName) fail("rules_scope", "/agentName");
    pointerRoot = "/jsonText";
    try { value = safeParse(value.jsonText, "json"); assertBoundedAcyclicGraph(value); } catch { fail("rules_schema", "/jsonText"); }
  }
  if (record(value) && Object.hasOwn(value, "previousDraft")) {
    const copy = { ...value };
    delete copy.previousDraft;
    value = copy;
  }
  let list: unknown[];
  let listBase = pointerRoot;
  if (Array.isArray(value)) list = value;
  else if (record(value) && Array.isArray(value.rules)) { list = value.rules; listBase = joinPointer(pointerRoot, "/rules"); }
  else if (record(value) && Array.isArray(value.rule)) { list = value.rule; listBase = joinPointer(pointerRoot, "/rule"); }
  else if (record(value) && record(value.rule)) list = [value];
  else if (record(value) && (Object.hasOwn(value, "action") || Object.hasOwn(value, "condition"))) list = [value];
  else fail("rules_schema");
  return list.map((item, index) => {
    const base = list.length === 1 && !Array.isArray(value) && record(value) && !Array.isArray(value.rules) && !Array.isArray(value.rule)
      ? pointerRoot : joinPointer(listBase, `/${index}`);
    return unpackRule(item, `${listBase}/${index}`, base, agentName);
  });
}

function requiredPaths(node: unknown, path: string, output: SelectorDeclaration[], errors: RuleCoverageDiagnostic[]): void {
  if (!record(node)) { errors.push({ code: "rules_schema", path, message: "Rule condition is invalid." }); return; }
  const composites = ["and", "or", "not"].filter(key => Object.hasOwn(node, key));
  const hasSelector = Object.hasOwn(node, "selector");
  const hasEvaluator = Object.hasOwn(node, "evaluator");
  if (composites.length) {
    if (composites.length !== 1 || hasSelector || hasEvaluator) { errors.push({ code: "rules_schema", path, message: "Rule condition is invalid." }); return; }
    const key = composites[0]!;
    if (key === "not") requiredPaths(node.not, `${path}/not`, output, errors);
    else if (!Array.isArray(node[key]) || (node[key] as unknown[]).length === 0) errors.push({ code: "rules_schema", path: `${path}/${key}`, message: "Rule condition is invalid." });
    else (node[key] as unknown[]).forEach((child, index) => requiredPaths(child, `${path}/${key}/${index}`, output, errors));
    return;
  }
  if (hasSelector || hasEvaluator) {
    if (!hasEvaluator || !record(node.evaluator) || typeof node.evaluator.name !== "string" || !record(node.evaluator.config)) {
      errors.push({ code: "rules_schema", path: `${path}/evaluator`, message: "Rule condition is invalid." }); return;
    }
    const evaluator = node.evaluator;
    const evaluatorConfig = evaluator.config as Record<string, unknown>;
    const semantic = evaluator.name === "semantic" || evaluator.name === "finish_claims";
    let selector = "*";
    const nullIsMissing = evaluator.name === "json" && evaluatorConfig.allow_null_required !== true;
    if (hasSelector) {
      if (!record(node.selector) || typeof node.selector.path !== "string") { errors.push({ code: "rules_schema", path: `${path}/selector/path`, message: "Rule selector is invalid." }); return; }
      selector = node.selector.path;
      if (!(semantic && selector === "*")) output.push({ path: `${path}/selector/path`, selector, nullIsMissing: false });
    } else if (!semantic) { errors.push({ code: "rules_schema", path: `${path}/selector`, message: "Rule selector is required." }); return; }
    if (evaluator.name === "json" && Array.isArray(evaluatorConfig.required_fields)) {
      for (const [index, field] of evaluatorConfig.required_fields.entries()) {
        if (typeof field !== "string") { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/required_fields/${index}`, message: "Rule dependency is invalid." }); continue; }
        output.push({ path: `${path}/evaluator/config/required_fields/${index}`, selector: joinSelector(selector, field), nullIsMissing });
      }
    }
    if (evaluator.name === "json" && !Array.isArray(evaluatorConfig.required_fields) && Object.hasOwn(evaluatorConfig, "required_fields")) {
      errors.push({ code: "rules_schema", path: `${path}/evaluator/config/required_fields`, message: "Rule dependencies are invalid." });
    }
    if (evaluator.name === "json") {
      for (const key of ["field_types", "field_constraints"] as const) {
        const fields = evaluatorConfig[key];
        if (fields === undefined || fields === null) continue;
        if (!record(fields)) {
          errors.push({ code: "rules_schema", path: `${path}/evaluator/config/${key}`, message: "Rule field dependencies are invalid." });
          continue;
        }
        for (const [fieldPath, fieldConfig] of Object.entries(fields)) {
          const pointer = `${path}/evaluator/config/${key}/${fieldPath.replace(/~/g, "~0").replace(/\//g, "~1")}`;
          if (!fieldPath.trim() || fieldPath.split(".").some(part => !part)) {
            errors.push({ code: "rules_schema", path: pointer, message: "Rule field path is invalid." });
            continue;
          }
          if (key === "field_types" && typeof fieldConfig !== "string") {
            errors.push({ code: "rules_schema", path: pointer, message: "Rule field type is invalid." });
            continue;
          }
          if (key === "field_constraints" && !record(fieldConfig)) {
            errors.push({ code: "rules_schema", path: pointer, message: "Rule field constraints are invalid." });
            continue;
          }
          // The evaluator reports absent dictionary paths, but this coverage
          // check intentionally does not infer type/range/$param semantics.
          output.push({ path: pointer, selector: joinSelector(selector, fieldPath), nullIsMissing: false });
        }
      }
    }
    if (evaluator.name === "json" && evaluatorConfig.allow_null_required !== undefined && typeof evaluatorConfig.allow_null_required !== "boolean") {
      errors.push({ code: "rules_schema", path: `${path}/evaluator/config/allow_null_required`, message: "Null dependency behavior is invalid." });
    }
    if (evaluator.name === "json" && Object.keys(evaluatorConfig).some(key => !["required_fields", "allow_null_required", "field_types", "field_constraints"].includes(key))) {
      output.push({ path: `${path}/evaluator/config`, selector: "*", forceUnknown: true });
    }
    if (evaluator.name === "semantic" && Array.isArray(evaluatorConfig.bindings)) {
      for (const [index, binding] of evaluatorConfig.bindings.entries()) {
        if (!record(binding)) { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}`, message: "Rule binding is invalid." }); continue; }
        if (binding.required !== undefined && typeof binding.required !== "boolean") { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}/required`, message: "Rule binding requirement is invalid." }); continue; }
        if (binding.max_age_seconds !== undefined && (typeof binding.max_age_seconds !== "number" || !Number.isFinite(binding.max_age_seconds) || binding.max_age_seconds < 0)) { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}/max_age_seconds`, message: "Rule binding age is invalid." }); continue; }
        if (typeof binding.path !== "string") { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}/path`, message: "Rule binding path is required." }); continue; }
        if (binding.max_age_seconds !== undefined && typeof binding.observed_at_path !== "string") { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}/observed_at_path`, message: "Timestamp binding path is required." }); continue; }
        if (binding.observed_at_path !== undefined && typeof binding.observed_at_path !== "string") { errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings/${index}/observed_at_path`, message: "Timestamp binding path is invalid." }); continue; }
        if (binding.required === false) continue;
        output.push({ path: `${path}/evaluator/config/bindings/${index}/path`, selector: binding.path, nullIsMissing: true, semanticArray: true });
        if (binding.max_age_seconds !== undefined) {
          output.push({ path: `${path}/evaluator/config/bindings/${index}/observed_at_path`, selector: binding.observed_at_path as string, nullIsMissing: true, semanticArray: true });
        }
      }
    }
    if (evaluator.name === "semantic" && !Array.isArray(evaluatorConfig.bindings) && Object.hasOwn(evaluatorConfig, "bindings")) {
      errors.push({ code: "rules_schema", path: `${path}/evaluator/config/bindings`, message: "Rule bindings are invalid." });
    }
    if (evaluator.name === "semantic" && (!Array.isArray(evaluatorConfig.bindings) || evaluatorConfig.bindings.length === 0)) {
      output.push({ path: `${path}/evaluator`, selector: "*", forceUnknown: true });
    }
    if (evaluator.name !== "json" && evaluator.name !== "semantic") output.push({ path: `${path}/evaluator`, selector: "*", forceUnknown: true });
    return;
  }
  errors.push({ code: "rules_schema", path, message: "Rule condition is invalid." });
}

function joinSelector(base: string, suffix: string): string { return base === "*" ? suffix : `${base}.${suffix}`; }

function selectorParts(selector: string): { root: string; parts: string[] } | undefined {
  if (!selector || /[*?[\]{}]/.test(selector) || /\s/.test(selector) || selector.includes("$") ) return undefined;
  const parts = selector.split(".");
  if (parts.some(part => !part || part === "..")) return undefined;
  return { root: parts[0]!, parts: parts.slice(1) };
}

function atPath(value: unknown, parts: readonly string[], nullIsMissing = false, semanticArray = false): CoverageStatus {
  let current = value;
  for (const part of parts) {
    if (current === UNKNOWN_VALUE) return "unknown";
    if (current === UNKNOWN_ARRAY) return "unknown";
    if (current === MISSING_VALUE) return "missing";
    if (Array.isArray(current)) {
      if (!semanticArray || !/^\d+$/.test(part)) return "missing";
      const index = Number(part);
      if (index >= current.length) return "missing";
      current = current[index];
      continue;
    }
    if (!record(current)) return "missing";
    if (!Object.hasOwn(current, part)) return Object.hasOwn(current, PARTIAL_OBJECT) ? "unknown" : "missing";
    current = current[part];
    if (current === null && parts.at(-1) !== part) return "missing";
  }
  if (current === UNKNOWN_VALUE || (record(current) && Object.hasOwn(current, PARTIAL_OBJECT))) return "unknown";
  if (current === UNKNOWN_ARRAY) return "covered";
  if (current === MISSING_VALUE) return "missing";
  return current === null && nullIsMissing ? "missing" : "covered";
}

function checkpointStages(): Array<"pre" | "post"> { return ["pre", "post"]; }

function scopeStatus(scope: Record<string, unknown> | undefined, checkpointName: string, checkpoint: Record<string, unknown>, stage: "pre" | "post"): "apply" | "skip" | "unknown" {
  if (!scope) return "apply";
  if (Array.isArray(scope.step_types) && !scope.step_types.includes(checkpoint.step_type ?? "tool")) return "skip";
  if (Array.isArray(scope.stages) && !scope.stages.includes(stage)) return "skip";
  if (Array.isArray(scope.step_names) && scope.step_names.includes(checkpointName)) return "apply";
  if (typeof scope.step_name_regex === "string") return "unknown";
  if (Array.isArray(scope.step_names)) return "skip";
  return "apply";
}

function checkpointRoot(config: InstrumentationConfig, checkpoint: Record<string, unknown>, root: string, stage: "pre" | "post"): { known: boolean; value?: unknown } {
  if (root === "input") {
    if (Object.hasOwn(checkpoint, "input_from")) {
      const source = checkpoint.input_from;
      if (source === "all_kwargs") return { known: false };
      if (source === "all_params") return Array.isArray(checkpoint.parameter_names) && checkpoint.parameter_names.length === 0
        ? { known: true, value: {} } : { known: false };
      if (record(source) && Object.hasOwn(source, "value")) return { known: true, value: source.value };
      return { known: false };
    }
    return checkpoint.step_type === "llm"
      ? { known: true, value: UNKNOWN_ARRAY }
      : { known: true, value: { args: UNKNOWN_ARRAY } };
  }
  if (root === "output") {
    if (stage === "pre") return { known: true, value: MISSING_VALUE };
    if (record(checkpoint.output_from) && Object.hasOwn(checkpoint.output_from, "value")) return { known: true, value: checkpoint.output_from.value };
    return { known: false };
  }
  if (root === "context") {
    const state = checkpoint.state;
    let configuredState: unknown = MISSING_VALUE;
    if (record(state)) configuredState = state;
    else if (record(checkpoint.state_from)) configuredState = Object.hasOwn(checkpoint.state_from, "value") ? checkpoint.state_from.value : UNKNOWN_VALUE;
    else if (record(checkpoint.options) && record(checkpoint.options.state)) {
      const optionState = checkpoint.options.state;
      configuredState = Object.keys(optionState).length === 1 && typeof optionState.binding === "string" ? UNKNOWN_VALUE : optionState;
    }
    else if (Object.hasOwn(checkpoint, "state_provider") || typeof checkpoint.options === "string" || (record(checkpoint.options) && Object.hasOwn(checkpoint.options, "state"))) configuredState = UNKNOWN_VALUE;
    else if (record(checkpoint.context_from) && Object.hasOwn(checkpoint.context_from, "value")) {
      const contextValue = checkpoint.context_from.value;
      configuredState = record(contextValue) && Object.hasOwn(contextValue, "state") ? contextValue.state : MISSING_VALUE;
    } else if (Object.hasOwn(checkpoint, "context_from")) configuredState = UNKNOWN_VALUE;
    else {
      const entrypointContexts = config.entrypoints.filter(entry => record(entry) && Object.hasOwn(entry, "context"));
      const entrypointContext = entrypointContexts.length === 1 ? entrypointContexts[0] : undefined;
      if (config.entrypoints.length > 1) configuredState = UNKNOWN_VALUE;
      else if (record(entrypointContext) && record(entrypointContext.context) && Object.hasOwn(entrypointContext.context, "value")) {
        const contextValue = entrypointContext.context.value;
        configuredState = record(contextValue) && Object.hasOwn(contextValue, "state") ? contextValue.state : MISSING_VALUE;
      } else if (entrypointContext || config.entrypoints.length > 1) configuredState = UNKNOWN_VALUE;
    }
    const context = { state: configuredState } as Record<string | symbol, unknown>;
    Object.defineProperty(context, PARTIAL_OBJECT, { value: true });
    return { known: true, value: context };
  }
  return { known: false };
}

function inputSchemaNodeAtPointer(root: unknown, reference: string): unknown {
  if (!reference.startsWith("#")) return undefined;
  let fragment: string;
  try { fragment = decodeURIComponent(reference.slice(1)); } catch { return undefined; }
  if (fragment === "") return root;
  if (!fragment.startsWith("/")) return undefined;
  let value = root;
  for (const rawToken of fragment.slice(1).split("/")) {
    if (/~(?![01])/.test(rawToken)) return undefined;
    const token = rawToken.replace(/~1/g, "/").replace(/~0/g, "~");
    if ((!record(value) && !Array.isArray(value)) || !Object.hasOwn(value, token)) return undefined;
    value = (value as Record<string, unknown>)[token];
  }
  return value;
}

function inputSchemaProjection(schemaValue: unknown, parts: readonly string[], nullIsMissing: boolean): CoverageStatus {
  const schemaRoot = schemaValue;
  const dereference = (candidate: unknown): unknown => {
    let schema = candidate;
    const seen = new Set<string>();
    while (record(schema) && Object.hasOwn(schema, "$ref")) {
      if (typeof schema.$ref !== "string" || seen.has(schema.$ref)) return UNKNOWN_VALUE;
      seen.add(schema.$ref);
      schema = inputSchemaNodeAtPointer(schemaRoot, schema.$ref);
      if (schema === undefined) return UNKNOWN_VALUE;
    }
    return schema;
  };
  const complex = (schema: Record<string, unknown>): boolean =>
    ["allOf", "anyOf", "oneOf", "not", "if", "then", "else", "dependencies", "patternProperties"].some(key => Object.hasOwn(schema, key));
  const terminalNullStatus = (candidate: unknown): CoverageStatus => {
    const schema = dereference(candidate);
    if (!record(schema) || complex(schema)) return "unknown";
    const type = schema.type;
    if (typeof type === "string") return type === "null" ? "missing" : "covered";
    if (Array.isArray(type)) {
      const types = type.filter(item => typeof item === "string") as string[];
      if (types.length !== type.length || !types.includes("null")) return types.length === type.length ? "covered" : "unknown";
      return types.length === 1 ? "missing" : "unknown";
    }
    return "unknown";
  };
  if (parts.length === 0) {
    const root = dereference(schemaRoot);
    if (!record(root) || complex(root) || root.type === undefined) return "unknown";
    return nullIsMissing ? terminalNullStatus(root) : "covered";
  }
  let current: unknown = schemaRoot;
  for (const [index, part] of parts.entries()) {
    const node = dereference(current);
    if (!record(node) || complex(node)) return "unknown";
    const type = node.type;
    const objectOnly = type === "object" || (Array.isArray(type) && type.length === 1 && type[0] === "object");
    if (!objectOnly) return "unknown";
    const properties = node.properties;
    if (record(properties) && Object.hasOwn(properties, part)) {
      if (!Array.isArray(node.required) || !node.required.includes(part)) return "unknown";
      current = properties[part];
      if (index === parts.length - 1) return nullIsMissing ? terminalNullStatus(current) : "covered";
      continue;
    }
    if (node.additionalProperties === false) return "missing";
    return "unknown";
  }
  return nullIsMissing ? terminalNullStatus(current) : "covered";
}

function selectorStatus(config: InstrumentationConfig, checkpoint: Record<string, unknown>, selector: string, stage: "pre" | "post", nullIsMissing = false, semanticArray = false): CoverageStatus {
  const parsed = selectorParts(selector);
  if (!parsed) return "unknown";
  const { root, parts } = parsed;
  if (!["input", "output", "context"].includes(root)) return "unknown";
  const source = checkpointRoot(config, checkpoint, root, stage);
  if (!source.known) {
    if (root === "input" && Object.hasOwn(checkpoint, "input_schema")) return inputSchemaProjection(checkpoint.input_schema, parts, nullIsMissing);
    return "unknown";
  }
  const staticStatus = atPath(source.value, parts, nullIsMissing, semanticArray);
  if (staticStatus !== "unknown" || root !== "input" || !Object.hasOwn(checkpoint, "input_schema")) return staticStatus;
  return inputSchemaProjection(checkpoint.input_schema, parts, nullIsMissing);
}

/** Analyze declared Rule paths against statically established checkpoint payload shapes. */
export function analyzeRuleCoverage(config: InstrumentationConfig, rules: RuleEntry[], configAgentName: string): { coverage: RuleCoverageItem[]; diagnostics: RuleCoverageDiagnostic[] } {
  const coverage: RuleCoverageItem[] = [];
  const diagnostics: RuleCoverageDiagnostic[] = [];
  for (const [ruleIndex, rule] of rules.entries()) {
    if (rule.agentName && rule.agentName !== configAgentName) { diagnostics.push({ code: "rules_scope", path: "/agentName", message: "Rule draft agent does not match the configuration." }); continue; }
    const scope = rule.scope ?? (record(rule.definition.scope) ? rule.definition.scope : undefined);
    const condition = rule.definition.condition;
    const ruleDiagnosticStart = diagnostics.length;
    const selectorDecls: SelectorDeclaration[] = [];
    if (record(condition)) requiredPaths(condition, joinPointer(rule.declarationBase, "/condition"), selectorDecls, diagnostics);
    else if (record(rule.definition) && record(rule.definition.selector) && record(rule.definition.evaluator)) {
      requiredPaths({ selector: rule.definition.selector, evaluator: rule.definition.evaluator }, rule.declarationBase, selectorDecls, diagnostics);
    } else diagnostics.push({ code: "rules_schema", path: joinPointer(rule.declarationBase, "/condition"), message: "Rule condition is required." });
    const isStringArray = (value: unknown): boolean => value === null || (Array.isArray(value) && value.length > 0 && value.every(item => typeof item === "string"));
    if ((rule.definition.enabled !== undefined && typeof rule.definition.enabled !== "boolean")
      || (scope !== undefined && scope !== null && (!record(scope) || Object.keys(scope).some(key => !["step_names", "step_types", "stages", "step_name_regex"].includes(key))
        || (scope.step_names !== undefined && !isStringArray(scope.step_names))
        || (scope.step_types !== undefined && !isStringArray(scope.step_types))
        || (scope.stages !== undefined && (scope.stages !== null && (!Array.isArray(scope.stages) || scope.stages.length === 0 || !scope.stages.every(value => value === "pre" || value === "post"))))
        || (scope.step_name_regex !== undefined && scope.step_name_regex !== null && typeof scope.step_name_regex !== "string")))) {
      diagnostics.push({ code: rule.definition.enabled !== undefined && typeof rule.definition.enabled !== "boolean" ? "rules_schema" : "rules_scope", path: rule.definition.enabled !== undefined && typeof rule.definition.enabled !== "boolean" ? joinPointer(rule.declarationBase, "/enabled") : rule.scopePath, message: "Rule scope is invalid." });
      continue;
    }
    if (diagnostics.length > ruleDiagnosticStart) continue;
    if (rule.definition.enabled === false) continue;
    const normalizedScope = record(scope) ? scope : undefined;
    let hadApplicable = false;
    const uncertainScopes: Array<{ checkpoint: string; stage: "pre" | "post" }> = [];
    for (const [checkpointName, item] of Object.entries(config.checkpoints)) {
      const checkpoint = item as unknown as Record<string, unknown>;
      for (const stage of checkpointStages()) {
        const applies = scopeStatus(normalizedScope, checkpointName, checkpoint, stage);
        if (applies === "skip") continue;
        if (applies === "unknown") { uncertainScopes.push({ checkpoint: checkpointName, stage }); continue; }
        hadApplicable = true;
        for (const declaration of selectorDecls) {
          const status = declaration.forceUnknown ? "unknown" : selectorStatus(config, checkpoint, declaration.selector, stage, Boolean(declaration.nullIsMissing), Boolean(declaration.semanticArray));
          coverage.push({ rule_index: ruleIndex, rule_path: declaration.path, checkpoint: checkpointName, stage, selector: declaration.selector, status });
        }
      }
    }
    if (!hadApplicable && uncertainScopes.length) {
      for (const uncertainScope of uncertainScopes) coverage.push({ rule_index: ruleIndex, rule_path: rule.scopePath, checkpoint: uncertainScope.checkpoint, stage: uncertainScope.stage, selector: "*", status: "unknown" });
    } else if (!hadApplicable) coverage.push({ rule_index: ruleIndex, rule_path: rule.scopePath, checkpoint: "", stage: "pre", selector: "*", status: "unknown" });
    else for (const uncertainScope of uncertainScopes) coverage.push({ rule_index: ruleIndex, rule_path: rule.scopePath, checkpoint: uncertainScope.checkpoint, stage: uncertainScope.stage, selector: "*", status: "unknown" });
  }
  coverage.sort((a, b) => a.rule_index - b.rule_index || compareText(a.checkpoint, b.checkpoint) || (a.stage === b.stage ? 0 : a.stage === "pre" ? -1 : 1) || compareText(a.rule_path, b.rule_path));
  for (const item of coverage) if (item.status !== "covered") diagnostics.push({
    code: item.status === "missing" ? "coverage_missing" : "coverage_unknown",
    path: item.rule_path,
    message: item.status === "missing" ? "A Rule selector path is absent from this configured checkpoint." : "A Rule selector path cannot be proven from static configuration.",
  });
  return { coverage, diagnostics };
}
