// Modified for Norma.
import Ajv, { type AnySchema, type ValidateFunction } from "ajv/dist/ajv.js";
import { configuredSourceCheckError } from "./checkpoint";

const draft7Uri = "http://json-schema.org/draft-07/schema#";
const ajv = new Ajv({ allErrors: true, strict: true, strictTypes: false, strictRequired: false, strictTuples: false, validateFormats: false });
const validators = new WeakMap<object, ValidateFunction>();
const draft7Keywords = new Set([
  "$schema", "$id", "$ref", "$comment", "title", "description", "default", "readOnly", "writeOnly", "examples",
  "multipleOf", "maximum", "exclusiveMaximum", "minimum", "exclusiveMinimum", "maxLength", "minLength", "pattern",
  "additionalItems", "items", "maxItems", "minItems", "uniqueItems", "contains", "maxProperties", "minProperties",
  "required", "additionalProperties", "definitions", "properties", "patternProperties", "dependencies", "propertyNames",
  "const", "enum", "type", "format", "allOf", "anyOf", "oneOf", "not", "if", "then", "else",
]);

function pointerPart(value: string): string { return value.replace(/~/g, "~0").replace(/\//g, "~1"); }
export class InputSchemaConfigurationError extends TypeError {
  readonly code = "input_schema";
  constructor(readonly path: string, readonly detail: string) {
    super(`Invalid input schema at '${path}': ${detail}.`);
    this.name = "InputSchemaConfigurationError";
  }
}
function schemaFailure(pointer: string, detail: string): InputSchemaConfigurationError {
  return new InputSchemaConfigurationError(pointer, detail);
}
function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function childSchemas(schema: Record<string, unknown>): Array<[string, unknown]> {
  const result: Array<[string, unknown]> = [];
  for (const keyword of ["properties", "patternProperties", "definitions"]) {
    const map = schema[keyword];
    if (object(map)) for (const [name, child] of Object.entries(map)) result.push([`${keyword}/${pointerPart(name)}`, child]);
  }
  for (const keyword of ["additionalProperties", "additionalItems", "not", "if", "then", "else", "contains", "propertyNames"]) {
    if (schema[keyword] !== undefined) result.push([keyword, schema[keyword]]);
  }
  const items = schema.items;
  if (Array.isArray(items)) items.forEach((child, index) => result.push([`items/${index}`, child]));
  else if (items !== undefined) result.push(["items", items]);
  for (const keyword of ["allOf", "anyOf", "oneOf"]) {
    const alternatives = schema[keyword];
    if (Array.isArray(alternatives)) alternatives.forEach((child, index) => result.push([`${keyword}/${index}`, child]));
  }
  const dependencies = schema.dependencies;
  if (object(dependencies)) for (const [name, child] of Object.entries(dependencies)) {
    if (!Array.isArray(child)) result.push([`dependencies/${pointerPart(name)}`, child]);
  }
  return result;
}
function pointerTokens(reference: string, configPointer: string): string[] {
  if (!reference.startsWith("#")) throw schemaFailure(configPointer, "only local JSON Pointer $ref values are supported");
  let fragment: string;
  try { fragment = decodeURIComponent(reference.slice(1)); }
  catch { throw schemaFailure(configPointer, "$ref contains an invalid URI fragment escape"); }
  if (fragment === "") return [];
  if (!fragment.startsWith("/")) throw schemaFailure(configPointer, "only local JSON Pointer $ref values are supported");
  return fragment.slice(1).split("/").map(token => {
    if (/~(?![01])/.test(token)) throw schemaFailure(configPointer, "$ref contains an invalid JSON Pointer escape");
    return token.replace(/~1/g, "/").replace(/~0/g, "~");
  });
}
function getAtPointer(root: unknown, tokens: string[], pointer: string): unknown {
  let value = root;
  for (const token of tokens) {
    if (!object(value) && !Array.isArray(value)) throw schemaFailure(pointer, "$ref does not resolve to a schema");
    if (!Object.hasOwn(value, token)) throw schemaFailure(pointer, "$ref does not resolve to a schema");
    value = (value as Record<string, unknown>)[token];
  }
  if (!(typeof value === "boolean" || object(value))) throw schemaFailure(pointer, "$ref does not resolve to a schema");
  return value;
}
function assertReferences(schema: unknown, configPointer: string): void {
  const graph = new Map<string, string[]>();
  const knownPaths = new Map<string, unknown>();
  const visited = new WeakSet<object>();
  const visit = (current: unknown, path: string): void => {
    if (typeof current === "boolean") { knownPaths.set(path, current); return; }
    if (!object(current)) throw schemaFailure(configPointer, `schema node at '${path || "/"}' must be an object or boolean`);
    if (visited.has(current)) return;
    visited.add(current);
    knownPaths.set(path, current);
    const refs: string[] = [];
    const children = childSchemas(current);
    if (Object.hasOwn(current, "$ref")) {
      const reference = current.$ref;
      if (typeof reference !== "string") throw schemaFailure(configPointer, "$ref must be a string");
      const tokens = pointerTokens(reference, configPointer);
      refs.push(tokens.length ? `/${tokens.map(pointerPart).join("/")}` : "");
      getAtPointer(schema, tokens, configPointer);
    }
    const childPaths: string[] = [];
    for (const [suffix, child] of children) {
      const childPath = path ? `${path}/${suffix}` : `/${suffix}`;
      childPaths.push(childPath);
      visit(child, childPath);
    }
    graph.set(path, [...childPaths, ...refs]);
  };
  visit(schema, "");
  // Resolve targets by canonical schema paths and follow only $ref edges. A
  // reference cycle is rejected rather than left to a validator's recursion rules.
  const state = new Map<string, 0 | 1 | 2>();
  const walk = (path: string): void => {
    const status = state.get(path) ?? 0;
    if (status === 1) throw schemaFailure(configPointer, "cyclic local $ref graph is not supported");
    if (status === 2) return;
    state.set(path, 1);
    for (const target of graph.get(path) ?? []) {
      if (!knownPaths.has(target)) throw schemaFailure(configPointer, "$ref target is not a schema node");
      walk(target);
    }
    state.set(path, 2);
  };
  for (const path of graph.keys()) walk(path);
}
function assertSupportedSchemaPositions(schema: unknown, configPointer: string): void {
  const visited = new WeakSet<object>();
  const visit = (current: unknown, root = false): void => {
    if (!object(current) || visited.has(current)) return;
    visited.add(current);
    if (Object.hasOwn(current, "$id")) throw schemaFailure(configPointer, "$id is not supported");
    if (Object.hasOwn(current, "format")) throw schemaFailure(configPointer, "format is not supported");
    if (!root && current.$schema !== undefined && current.$schema !== draft7Uri) {
      throw schemaFailure(configPointer, `only Draft 7 ('${draft7Uri}') is supported`);
    }
    for (const key of Object.keys(current)) if (!draft7Keywords.has(key)) throw schemaFailure(configPointer, `unsupported keyword '${key}'`);
    for (const [, child] of childSchemas(current)) visit(child);
  };
  visit(schema, true);
}

const MAX_SCHEMA_SOURCE_NODES = 100_000;
const MAX_EXPANDED_SCHEMA_NODES = 4_096;
interface InlineContext { active: Set<string>; memo: Map<string, unknown>; nodes: number }
function inlineDraft7Refs(current: unknown, root: unknown, context: InlineContext, depth = 0): unknown {
  if (depth > 100 || ++context.nodes > MAX_SCHEMA_SOURCE_NODES) throw schemaFailure("input_schema", "schema graph exceeds the supported depth or size");
  if (typeof current === "boolean") return current;
  if (!object(current)) return current;
  if (Object.hasOwn(current, "$ref")) {
    const reference = current.$ref as string;
    const tokens = pointerTokens(reference, "input_schema");
    const targetPath = tokens.length ? `/${tokens.map(pointerPart).join("/")}` : "";
    if (context.active.has(targetPath)) throw schemaFailure("input_schema", "cyclic local $ref graph is not supported");
    const cached = context.memo.get(targetPath);
    if (cached !== undefined) return cached;
    context.active.add(targetPath);
    try {
      const inlined = inlineDraft7Refs(getAtPointer(root, tokens, "input_schema"), root, context, depth + 1);
      context.memo.set(targetPath, inlined);
      return inlined;
    } finally { context.active.delete(targetPath); }
  }
  const result: Record<string, unknown> = { ...current };
  for (const keyword of ["properties", "patternProperties", "definitions"]) {
    const map = current[keyword];
    if (object(map)) result[keyword] = Object.fromEntries(Object.entries(map).map(([name, child]) => [name, inlineDraft7Refs(child, root, context, depth + 1)]));
  }
  for (const keyword of ["additionalProperties", "additionalItems", "not", "if", "then", "else", "contains", "propertyNames"]) {
    if (current[keyword] !== undefined) result[keyword] = inlineDraft7Refs(current[keyword], root, context, depth + 1);
  }
  const items = current.items;
  if (Array.isArray(items)) result.items = items.map(child => inlineDraft7Refs(child, root, context, depth + 1));
  else if (items !== undefined) result.items = inlineDraft7Refs(items, root, context, depth + 1);
  for (const keyword of ["allOf", "anyOf", "oneOf"]) {
    if (Array.isArray(current[keyword])) result[keyword] = (current[keyword] as unknown[]).map(child => inlineDraft7Refs(child, root, context, depth + 1));
  }
  const dependencies = current.dependencies;
  if (object(dependencies)) result.dependencies = Object.fromEntries(Object.entries(dependencies).map(([name, child]) => [name, Array.isArray(child) ? child : inlineDraft7Refs(child, root, context, depth + 1)]));
  return result;
}
function assertBoundedExpandedSchema(schema: unknown): void {
  const pending: Array<{ value: unknown; depth: number }> = [{ value: schema, depth: 0 }];
  let nodes = 0;
  while (pending.length) {
    const { value, depth } = pending.pop()!;
    if (depth > 100 || ++nodes > MAX_EXPANDED_SCHEMA_NODES) {
      throw schemaFailure("input_schema", "schema graph exceeds the supported depth or size");
    }
    if (value && typeof value === "object") {
      const children = Array.isArray(value) ? value : Object.values(value);
      for (const child of children) pending.push({ value: child, depth: depth + 1 });
    }
  }
}
export function compileInputSchema(schema: unknown, owner: object, configPointer: string): ValidateFunction {
  if (!object(schema)) throw schemaFailure(configPointer, "the schema must be an object");
  if (schema.$schema !== undefined && schema.$schema !== draft7Uri) {
    throw schemaFailure(configPointer, `only Draft 7 ('${draft7Uri}') is supported`);
  }
  try {
    assertSupportedSchemaPositions(schema, configPointer);
    assertReferences(schema, configPointer);
    const validationSchema = inlineDraft7Refs(schema, schema, { active: new Set(), memo: new Map(), nodes: 0 }) as AnySchema;
    assertBoundedExpandedSchema(validationSchema);
    const validate = ajv.compile(validationSchema);
    validators.set(owner, validate);
    return validate;
  } catch (error) {
    if (error instanceof InputSchemaConfigurationError) {
      if (error.path === configPointer) throw error;
      throw schemaFailure(configPointer, error.detail);
    }
    // Ajv's message may include schema values; expose only the safe config location.
    throw schemaFailure(configPointer, "schema is invalid or uses an unsupported keyword");
  }
}
function firstInstancePath(validate: ValidateFunction): string {
  const error = validate.errors?.[0];
  const path = error?.instancePath ?? "";
  const missing = error?.keyword === "required" ? error.params.missingProperty : undefined;
  const additional = error?.keyword === "additionalProperties" ? error.params.additionalProperty : undefined;
  const leaf = typeof missing === "string" ? missing : typeof additional === "string" ? additional : undefined;
  const fullPath = leaf === undefined ? path : `${path}/${pointerPart(leaf)}`;
  return fullPath || "/";
}
export function validateLiteralInput(schema: ValidateFunction, value: unknown, configPointer: string): void {
  if (!schema(value)) throw schemaFailure(configPointer, `literal input is invalid at instance path '${firstInstancePath(schema)}'`);
}
export function validateCapturedInput(owner: object, value: unknown, stepName: string): void {
  const validate = validators.get(owner);
  if (!validate) {
    throw configuredSourceCheckError("pre",
      `Checkpoint '${stepName}' input schema '/checkpoints/${pointerPart(stepName)}/input_schema' is unavailable; input validation was blocked.`);
  }
  if (validate(value)) return;
  const instancePath = firstInstancePath(validate);
  throw configuredSourceCheckError("pre",
    `Checkpoint '${stepName}' input was rejected by schema '/checkpoints/${pointerPart(stepName)}/input_schema' at instance path '${instancePath}'.`);
}

export function prepareInstrumentationInputSchemas(config: { version: number; checkpoints: Record<string, { input_schema?: unknown; input_from?: unknown }> }): void {
  if (config.version !== 2) return;
  for (const [name, checkpoint] of Object.entries(config.checkpoints)) {
    if (checkpoint.input_schema === undefined) continue;
    const base = `/checkpoints/${pointerPart(name)}`;
    const validate = compileInputSchema(checkpoint.input_schema, checkpoint, `${base}/input_schema`);
    const inputFrom = checkpoint.input_from;
    if (inputFrom && typeof inputFrom === "object" && !Array.isArray(inputFrom) && Object.hasOwn(inputFrom, "value")) {
      validateLiteralInput(validate, (inputFrom as { value: unknown }).value, `${base}/input_from/value`);
    }
  }
}
