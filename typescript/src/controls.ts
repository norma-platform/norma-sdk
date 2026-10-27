// Modified for Norma.
import { copyCheckpointArguments } from "./checkpoint-arguments";
import type { JsonObject, JsonValue } from "./types";

export type ControlType = "number" | "boolean" | "string" | "enum" | "string_list";
export type ControlStatus = "valid" | "invalid" | "requires_server_validation";
export type ControlSourceValidation = "admitted" | "invalid" | "unavailable";
export type ControlApplicabilityOperator = "present" | "nonempty" | "has_string_enum" | "not_false";

interface ControlDeclarationBase {
  label: string;
  description?: string;
}

export interface NumberControlDeclaration extends ControlDeclarationBase {
  type: "number";
  default: number;
  minimum: number;
  maximum: number;
  integer?: boolean;
}

export interface BooleanControlDeclaration extends ControlDeclarationBase {
  type: "boolean";
  default: boolean;
}

export interface StringControlDeclaration extends ControlDeclarationBase {
  type: "string";
  default: string;
  min_length?: number | null;
  max_length?: number | null;
}

export interface EnumControlDeclaration extends ControlDeclarationBase {
  type: "enum";
  default: string;
  allowed_values: string[];
}

export interface StringListControlDeclaration extends ControlDeclarationBase {
  type: "string_list";
  default: string[];
  allowed_values?: string[] | null;
  min_items?: number | null;
  max_items?: number | null;
}

export type ControlDeclaration = NumberControlDeclaration | BooleanControlDeclaration | StringControlDeclaration | EnumControlDeclaration | StringListControlDeclaration;

export interface ControlExpectedValueSchema {
  types: ControlType[];
  minimum?: number | null;
  maximum?: number | null;
  integer?: boolean | null;
  min_length?: number | null;
  max_length?: number | null;
  min_items?: number | null;
  max_items?: number | null;
  item_max_length?: number | null;
  allowed_values?: string[] | null;
}

export interface ControlApplicabilityPredicate {
  path_template: string;
  operator: ControlApplicabilityOperator;
}

export interface ControlSlotDescriptor {
  evaluator: string;
  evaluator_version: string;
  schema_fingerprint: string;
  path_template: string;
  control_types: ControlType[];
  predicates: Array<[string, JsonValue]>;
  applicability: ControlApplicabilityPredicate[];
  affects: "decision" | "assessment";
  expected_value_schema: ControlExpectedValueSchema;
  requires_server_validation: boolean;
}

export interface ControlEvaluatorManifest {
  evaluator: string;
  evaluator_version: string;
  schema_fingerprint: string;
  slots: ControlSlotDescriptor[];
}

export interface TrustedControlCapabilities {
  contract: "rule-controls-v1";
  fingerprint: string;
  manifests: ControlEvaluatorManifest[];
}

export interface ControlDiagnostic {
  readonly code: string;
  readonly sourcePointer: string;
  readonly message: string;
}

export interface ControlReference {
  readonly key: string;
  readonly sourcePointer: string;
  readonly destinationPointer: string | null;
  readonly evaluatorPointer: string | null;
}

export interface ControlValidationResult {
  readonly status: ControlStatus;
  readonly sourceValidation: ControlSourceValidation;
  readonly requiresServerValidation: boolean;
  readonly diagnostics: readonly ControlDiagnostic[];
  readonly authoredDocument: Readonly<JsonObject> | null;
  readonly references: readonly ControlReference[];
}

const contract = "rule-controls-v1";
const maxSourceBytes = 1_048_576;
const maxDepth = 64;
const maxNodes = 10_000;
const maxReferences = 256;
const maxDiagnostics = 50;
const maxEffectiveBytes = 16_384;
const maxStringScalars = 4_096;
const maxListItems = 128;
const controlTypes = new Set<ControlType>(["number", "boolean", "string", "enum", "string_list"]);
const forbiddenKeys = new Set(["__proto__", "prototype", "constructor"]);

class SourceParseError extends Error {
  public readonly duplicate: boolean;

  public constructor(duplicate = false) {
    super("Invalid JSON source");
    this.duplicate = duplicate;
  }
}

class StrictJsonParser {
  private offset = 0;
  private nodes = 0;
  private readonly floatPaths = new Set<string>();

  public constructor(private readonly source: string) {}

  public parse(): { value: JsonValue; floatPaths: ReadonlySet<string> } {
    const value = this.parseValue("", 0);
    this.skipWhitespace();
    if (this.offset !== this.source.length) throw new SourceParseError();
    return { value, floatPaths: this.floatPaths };
  }

  private parseValue(path: string, depth: number): JsonValue {
    this.nodes += 1;
    if (this.nodes > maxNodes || depth > maxDepth) throw new SourceParseError();
    this.skipWhitespace();
    const next = this.source[this.offset];
    if (next === "{") return this.parseObject(path, depth);
    if (next === "[") return this.parseArray(path, depth);
    if (next === '"') return this.parseString();
    if (next === "t" && this.source.slice(this.offset, this.offset + 4) === "true") {
      this.offset += 4;
      return true;
    }
    if (next === "f" && this.source.slice(this.offset, this.offset + 5) === "false") {
      this.offset += 5;
      return false;
    }
    if (next === "n" && this.source.slice(this.offset, this.offset + 4) === "null") {
      this.offset += 4;
      return null;
    }
    return this.parseNumber(path);
  }

  private parseObject(path: string, depth: number): JsonObject {
    this.offset += 1;
    this.skipWhitespace();
    const result: JsonObject = {};
    const seen = new Set<string>();
    if (this.source[this.offset] === "}") {
      this.offset += 1;
      return result;
    }
    while (this.offset <= this.source.length) {
      this.skipWhitespace();
      if (this.source[this.offset] !== '"') throw new SourceParseError();
      const key = this.parseString();
      if (seen.has(key)) throw new SourceParseError(true);
      seen.add(key);
      this.skipWhitespace();
      if (this.source[this.offset] !== ":") throw new SourceParseError();
      this.offset += 1;
      const value = this.parseValue(`${path}/${escapePointer(key)}`, depth + 1);
      Object.defineProperty(result, key, {
        value, enumerable: true, configurable: true, writable: true,
      });
      this.skipWhitespace();
      const delimiter = this.source[this.offset];
      if (delimiter === "}") {
        this.offset += 1;
        return result;
      }
      if (delimiter !== ",") throw new SourceParseError();
      this.offset += 1;
    }
    throw new SourceParseError();
  }

  private parseArray(path: string, depth: number): JsonValue[] {
    this.offset += 1;
    this.skipWhitespace();
    const result: JsonValue[] = [];
    if (this.source[this.offset] === "]") {
      this.offset += 1;
      return result;
    }
    while (this.offset <= this.source.length) {
      result.push(this.parseValue(`${path}/${result.length}`, depth + 1));
      this.skipWhitespace();
      const delimiter = this.source[this.offset];
      if (delimiter === "]") {
        this.offset += 1;
        return result;
      }
      if (delimiter !== ",") throw new SourceParseError();
      this.offset += 1;
    }
    throw new SourceParseError();
  }

  private parseString(): string {
    const start = this.offset;
    this.offset += 1;
    while (this.offset < this.source.length) {
      const char = this.source[this.offset];
      if (char === '"') {
        this.offset += 1;
        try {
          return JSON.parse(this.source.slice(start, this.offset)) as string;
        } catch {
          throw new SourceParseError();
        }
      }
      if (char === "\\") {
        this.offset += 1;
        if (this.offset >= this.source.length) throw new SourceParseError();
      } else if (char.charCodeAt(0) < 0x20) {
        throw new SourceParseError();
      }
      this.offset += 1;
    }
    throw new SourceParseError();
  }

  private parseNumber(path: string): number {
    const rest = this.source.slice(this.offset);
    const match = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(rest);
    if (!match) throw new SourceParseError();
    const token = match[0];
    const value = Number(token);
    if (!Number.isFinite(value)) throw new SourceParseError();
    if (/[.eE]/.test(token) || !Number.isSafeInteger(value)) this.floatPaths.add(path);
    this.offset += token.length;
    return Object.is(value, -0) ? 0 : value;
  }

  private skipWhitespace(): void {
    while ([" ", "\t", "\n", "\r"].includes(this.source[this.offset] ?? "")) this.offset += 1;
  }
}

export function validateAuthoredControls(
  source: string | Readonly<Record<string, unknown>>,
  trustedCapabilities?: TrustedControlCapabilities | Readonly<Record<string, unknown>>,
): ControlValidationResult {
  const diagnostics: ControlDiagnostic[] = [];
  let sourceValidation: ControlSourceValidation = "admitted";
  let document: JsonValue;
  let floatPaths: ReadonlySet<string> = new Set();

  if (typeof source === "string") {
    const bytes = new TextEncoder().encode(source);
    if (bytes.length > maxSourceBytes || !hasUnicodeScalars(source)) {
      return result("invalid", "invalid", false, [diag("control_source_too_large", "", "Source is outside strict JSON bounds.")], null);
    }
    try {
      const parsed = new StrictJsonParser(source).parse();
      document = parsed.value;
      floatPaths = parsed.floatPaths;
    } catch (error) {
      const code = error instanceof SourceParseError && error.duplicate
        ? "control_source_duplicate_key"
        : "control_source_invalid_json";
      return result("invalid", "invalid", false, [diag(code, "", "Source is not strict JSON.")], null);
    }
  } else {
    sourceValidation = "unavailable";
    try {
      const boundedCopy = copyParsedControlsJson(source);
      if (new TextEncoder().encode(JSON.stringify(boundedCopy)).length > maxSourceBytes) throw new TypeError("Source exceeds byte limit");
      document = copyCheckpointArguments([boundedCopy], false)[0] as JsonValue;
    } catch {
      return result("invalid", "invalid", false, [diag("control_source_invalid_json", "", "Parsed source is not plain JSON data.")], null);
    }
    diagnostics.push(diag("control_source_admission_unavailable", "", "Duplicate keys and original number spellings cannot be checked after parsing."));
  }

  if (!isRecord(document)) {
    return result("invalid", sourceValidation, false, [...diagnostics, diag("control_source_invalid_shape", "", "Authored source must be an object.")], null);
  }
  try {
    validateTree(document);
  } catch {
    return result("invalid", "invalid", false, [...diagnostics, diag("control_source_invalid_json", "", "Source is outside strict JSON bounds.")], null);
  }
  const retiredField = ["template", "template_values", "parameters"].find((key) => Object.hasOwn(document, key));
  if (retiredField) {
    return result("invalid", sourceValidation, false,
      [diag("control_declaration_invalid", `/${escapePointer(retiredField)}`, "Unsupported Rule field.")], freezeJson(document));
  }

  const rawControls = document.controls;
  if (Object.hasOwn(document, "controls") && !isRecord(rawControls)) {
    diagnostics.push(diag("control_declaration_invalid", "/controls", "Root controls must be an object."));
  }
  const declarations = isRecord(rawControls) ? rawControls : {};
  if (Object.keys(declarations).length > 32) diagnostics.push(diag("control_declaration_invalid", "/controls", "At most 32 declarations are supported."));
  for (const [key, declaration] of Object.entries(declarations)) validateDeclaration(key, declaration, diagnostics);

  const { references, malformed } = findReferences(document);
  if (malformed) diagnostics.push(malformed);
  if ((Object.hasOwn(document, "controls") || references.length > 0) && document.execution !== "server") {
    diagnostics.push(diag("control_execution_unsupported", "/execution", "Authored controls require server execution."));
  }
  if (references.length > maxReferences) diagnostics.push(diag("control_declaration_invalid", "/", "At most 256 control references are supported."));
  const evaluators = findEvaluators(document);
  const sourceValidReferences: Reference[] = [];
  for (const reference of references) {
    if (!Object.hasOwn(declarations, reference.key)) {
      diagnostics.push(diag("control_reference_unknown", reference.pointer, "Reference names no declared control."));
      continue;
    }
    if (!evaluators.some((item) => reference.pointer.startsWith(`${item.pointer}/config/`))) {
      diagnostics.push(diag("control_reference_not_allowed", reference.pointer, "Reference is outside a supported evaluator configuration."));
      continue;
    }
    sourceValidReferences.push(reference);
  }
  const used = new Set(references.map((reference) => reference.key));
  for (const key of Object.keys(declarations)) {
    if (!used.has(key)) diagnostics.push(diag("control_unused", `/controls/${escapePointer(key)}`, "Declared control is not used by a reference."));
  }
  let requiresServer = sourceValidation === "unavailable";
  const publicReferences = references.slice(0, maxReferences).map((reference) => {
    const evaluator = evaluators.find((item) => reference.pointer.startsWith(`${item.pointer}/config/`));
    return Object.freeze({
      key: reference.key,
      sourcePointer: reference.pointer,
      destinationPointer: evaluator ? reference.pointer : null,
      evaluatorPointer: evaluator?.pointer ?? null,
    });
  });
  if (sourceValidReferences.length > 0) {
    if (!capabilitiesShapeValid(trustedCapabilities)) {
      diagnostics.push(diag("control_capability_unavailable", "/", "Trusted rule-controls-v1 capabilities were not supplied."));
      requiresServer = true;
    } else {
      for (const reference of sourceValidReferences.slice(0, maxReferences)) {
        requiresServer = checkReference(reference, declarations, evaluators, trustedCapabilities.manifests, diagnostics) || requiresServer;
      }
    }
  }

  const effectiveValues: Record<string, JsonValue> = {};
  for (const [key, declaration] of Object.entries(declarations)) {
    if (isRecord(declaration) && Object.hasOwn(declaration, "default")) {
      Object.defineProperty(effectiveValues, key, { value: declaration.default, enumerable: true, configurable: true, writable: true });
      const valuePath = `/controls/${escapePointer(key)}/default`;
      if ([...floatPaths].some((path) => path === valuePath || path.startsWith(`${valuePath}/`))) requiresServer = true;
    }
  }
  const uncertainValueNumber = Object.keys(declarations).some((key) => {
    const path = `/controls/${escapePointer(key)}/default`;
    return [...floatPaths].some((entry) => entry === path || entry.startsWith(`${path}/`));
  });
  if (!uncertainValueNumber) {
    const effectiveBytes = new TextEncoder().encode(JSON.stringify(effectiveValues)).length;
    if (effectiveBytes > maxEffectiveBytes) diagnostics.push(diag("control_declaration_invalid", "/controls", "Effective control values exceed 16 KiB."));
  }
  if (uncertainValueNumber) {
    diagnostics.push(diag("control_serialization_requires_server_validation", "/controls", "Floating-point value serialization must be checked by server preparation."));
    requiresServer = true;
  }

  const bounded = boundDiagnostics(diagnostics);
  const invalid = bounded.some((item) => ![
    "control_source_admission_unavailable",
    "control_capability_unavailable",
    "control_serialization_requires_server_validation",
  ].includes(item.code));
  const status: ControlStatus = invalid ? "invalid" : requiresServer ? "requires_server_validation" : "valid";
  return result(status, sourceValidation, requiresServer, bounded, freezeJson(document), publicReferences);
}

function result(
  status: ControlStatus,
  sourceValidation: ControlSourceValidation,
  requiresServerValidation: boolean,
  diagnostics: ControlDiagnostic[],
  authoredDocument: Readonly<JsonObject> | null,
  references: readonly ControlReference[] = [],
): ControlValidationResult {
  return Object.freeze({
    status,
    sourceValidation,
    requiresServerValidation,
    diagnostics: Object.freeze(diagnostics),
    authoredDocument,
    references: Object.freeze([...references]),
  });
}

function diag(code: string, sourcePointer: string, message: string): ControlDiagnostic {
  return Object.freeze({ code: code.slice(0, 96), sourcePointer: sourcePointer.slice(0, 1024), message: message.slice(0, 256) });
}

function boundDiagnostics(items: ControlDiagnostic[]): ControlDiagnostic[] {
  return items.length <= maxDiagnostics
    ? items
    : [...items.slice(0, maxDiagnostics - 1), diag("control_diagnostics_truncated", "/", "Additional diagnostics were omitted.")];
}

function validateTree(root: JsonValue): void {
  const pending: Array<{ value: JsonValue; depth: number }> = [{ value: root, depth: 0 }];
  let nodes = 0;
  while (pending.length > 0) {
    const item = pending.pop();
    if (!item) continue;
    nodes += 1;
    if (nodes > maxNodes || item.depth > maxDepth) throw new TypeError("Source exceeds strict bounds");
    if (typeof item.value === "string" && !hasUnicodeScalars(item.value)) throw new TypeError("Invalid Unicode");
    if (typeof item.value === "number" && !Number.isFinite(item.value)) throw new TypeError("Invalid number");
    if (Array.isArray(item.value)) {
      for (const child of item.value) pending.push({ value: child, depth: item.depth + 1 });
    } else if (isRecord(item.value)) {
      for (const [key, child] of Object.entries(item.value)) {
        if (!hasUnicodeScalars(key)) throw new TypeError("Invalid Unicode key");
        pending.push({ value: child, depth: item.depth + 1 });
      }
    }
  }
}

function copyParsedControlsJson(source: unknown): Record<string, JsonValue> {
  let nodes = 0;
  let utf8StringBytes = 0;
  const active = new Set<object>();
  const copies = new Map<object, JsonValue>();
  const copy = (value: unknown, depth: number): JsonValue => {
    nodes += 1;
    if (nodes > maxNodes || depth > maxDepth) throw new TypeError("Source exceeds JSON complexity limits");
    if (value === null || typeof value === "string" || typeof value === "boolean") {
      if (typeof value === "string") {
        if (!hasUnicodeScalars(value) || value.length > maxSourceBytes) throw new TypeError("Invalid or oversized string");
        utf8StringBytes += new TextEncoder().encode(value).length;
        if (utf8StringBytes > maxSourceBytes) throw new TypeError("Source exceeds byte limit");
      }
      return value;
    }
    if (typeof value === "number") {
      if (!Number.isFinite(value)) throw new TypeError("Non-finite number");
      return Object.is(value, -0) ? 0 : value;
    }
    if (typeof value !== "object") throw new TypeError("Not JSON data");
    if (active.has(value)) throw new TypeError("Circular source");
    const previous = copies.get(value);
    if (previous !== undefined) return previous;
    const array = Array.isArray(value);
    const prototype = Object.getPrototypeOf(value);
    if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) throw new TypeError("Not a plain JSON object");
    const descriptors = Object.getOwnPropertyDescriptors(value);
    const keys = Reflect.ownKeys(descriptors);
    if (keys.some((key) => typeof key !== "string")) throw new TypeError("Symbol properties are not JSON");
    const output: JsonValue[] | JsonObject = array ? [] : Object.create(prototype === null ? null : Object.prototype) as JsonObject;
    if (array) {
      const length = descriptors.length?.value;
      if (typeof length !== "number" || keys.length !== length + 1) throw new TypeError("Sparse array");
      for (let index = 0; index < length; index += 1) if (!Object.hasOwn(descriptors, String(index))) throw new TypeError("Sparse array");
    }
    active.add(value);
    copies.set(value, output as JsonValue);
    for (const key of keys) {
      if (array && key === "length") continue;
      if (typeof key !== "string" || key.length > maxSourceBytes) throw new TypeError("Invalid or oversized key");
      utf8StringBytes += new TextEncoder().encode(key).length;
      if (utf8StringBytes > maxSourceBytes) throw new TypeError("Source exceeds byte limit");
      const descriptor = descriptors[key as string];
      if (!descriptor?.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError("Accessor property");
      const child = copy(descriptor.value, depth + 1);
      if (array) (output as JsonValue[]).push(child);
      else Object.defineProperty(output, key, { value: child, enumerable: true, configurable: true, writable: true });
    }
    active.delete(value);
    return output as JsonValue;
  };
  const result = copy(source, 0);
  if (!isRecord(result)) throw new TypeError("Authored rule source must be an object");
  return result;
}

interface Reference { pointer: string; key: string }
interface Evaluator { pointer: string; name: string; config: Record<string, JsonValue> }

function findReferences(document: Record<string, JsonValue>): { references: Reference[]; malformed: ControlDiagnostic | null } {
  const pending: Array<{ value: JsonValue; pointer: string }> = [{ value: document, pointer: "" }];
  const references: Reference[] = [];
  while (pending.length > 0 && references.length <= maxReferences) {
    const item = pending.pop();
    if (!item) continue;
    if (Array.isArray(item.value)) {
      item.value.forEach((child, index) => pending.push({ value: child, pointer: `${item.pointer}/${index}` }));
    } else if (isRecord(item.value)) {
      if (Object.hasOwn(item.value, "$param")) {
        if (Object.keys(item.value).length !== 1 || typeof item.value.$param !== "string") {
          return { references, malformed: diag("control_reference_invalid", item.pointer || "/", "Reserved reference must be exactly an object containing a string $param.") };
        }
        references.push({ pointer: item.pointer || "/", key: item.value.$param });
      } else {
        for (const [key, child] of Object.entries(item.value)) {
          if (item.pointer === "" && key === "controls") continue;
          pending.push({ value: child, pointer: `${item.pointer}/${escapePointer(key)}` });
        }
      }
    }
  }
  return { references, malformed: null };
}

function findEvaluators(document: Record<string, JsonValue>): Evaluator[] {
  const definition = document;
  const base = "";
  const root = definition.condition !== undefined ? { value: definition.condition, pointer: `${base}/condition` } :
    definition.selector !== undefined && definition.evaluator !== undefined ? { value: definition, pointer: base } : null;
  const output: Evaluator[] = [];
  if (!root) return output;
  const visit = (node: JsonValue, pointer: string, depth: number): void => {
    if (depth > maxDepth || !isRecord(node)) return;
    const composites = ["and", "or", "not"].filter((key) => Object.hasOwn(node, key));
    if (Object.hasOwn(node, "selector") && Object.hasOwn(node, "evaluator") && composites.length === 0) {
      const evaluator = node.evaluator;
      if (isRecord(evaluator) && typeof evaluator.name === "string" && isRecord(evaluator.config)) {
        output.push({ pointer: `${pointer}/evaluator`, name: evaluator.name, config: evaluator.config });
      }
      return;
    }
    if (composites.length !== 1 || Object.hasOwn(node, "selector") || Object.hasOwn(node, "evaluator")) return;
    const operator = composites[0];
    if (!operator) return;
    const child = node[operator];
    if ((operator === "and" || operator === "or") && Array.isArray(child) && child.length > 0) {
      child.forEach((value, index) => visit(value, `${pointer}/${operator}/${index}`, depth + 1));
    } else if (operator === "not" && isRecord(child)) visit(child, `${pointer}/not`, depth + 1);
  };
  visit(root.value, root.pointer, 0);
  return output;
}

function checkReference(
  reference: Reference,
  declarations: Record<string, JsonValue>,
  evaluators: Evaluator[],
  manifests: unknown[],
  diagnostics: ControlDiagnostic[],
): boolean {
  if (!Object.hasOwn(declarations, reference.key)) {
    diagnostics.push(diag("control_reference_unknown", reference.pointer, "Reference names no declared control."));
    return false;
  }
  const evaluator = evaluators.find((item) => reference.pointer.startsWith(`${item.pointer}/config/`));
  if (!evaluator) {
    diagnostics.push(diag("control_reference_not_allowed", reference.pointer, "Reference is outside a supported evaluator configuration."));
    return false;
  }
  const manifest = manifests.find((item) => isRecord(item) && item.evaluator === evaluator.name);
  if (!isRecord(manifest)) {
    diagnostics.push(diag("control_capability_unavailable", reference.pointer, "No trusted manifest is available for this evaluator."));
    return true;
  }
  if (!Array.isArray(manifest.slots)) {
    diagnostics.push(diag("control_capability_unavailable", reference.pointer, "Evaluator slot metadata is malformed."));
    return true;
  }
  const configPointer = `${evaluator.pointer}/config`;
  const actualPath = `/config${reference.pointer.slice(configPointer.length)}`;
  let unknownPredicate = false;
  for (const candidate of manifest.slots) {
    if (!isRecord(candidate) || typeof candidate.path_template !== "string") {
      unknownPredicate = true;
      continue;
    }
    const matched = matchPath(candidate.path_template, actualPath);
    if (!matched) continue;
    const [allowed, unknown] = matchesPredicates(candidate, evaluator.config, actualPath);
    unknownPredicate ||= unknown;
    if (!allowed) continue;
    if (!declarationFitsSlot(declarations[reference.key], candidate)) {
      diagnostics.push(diag("control_destination_invalid", reference.pointer, "Declaration does not satisfy the trusted destination value schema."));
    }
    return candidate.requires_server_validation !== false;
  }
  if (unknownPredicate) {
    diagnostics.push(diag("control_capability_unavailable", reference.pointer, "A trusted applicability predicate is unknown or malformed."));
    return true;
  }
  diagnostics.push(diag("control_reference_not_allowed", reference.pointer, "No trusted control slot allows this destination."));
  return false;
}

function matchPath(template: string, actual: string): boolean {
  const expected = template.split("/").slice(1);
  const parts = actual.split("/").slice(1);
  return expected.length === parts.length && expected.every((part, index) => part === "*" || part === parts[index]);
}

function matchesPredicates(slot: Record<string, JsonValue>, config: Record<string, JsonValue>, actualPath: string): [boolean, boolean] {
  if (slot.predicates !== undefined) {
    if (!Array.isArray(slot.predicates)) return [false, true];
    for (const predicate of slot.predicates) {
      if (!Array.isArray(predicate) || predicate.length !== 2 || typeof predicate[0] !== "string") return [false, true];
      const pointer = wildcardPath(predicate[0], actualPath).replace(/^\/config/, "");
      const found = pointerValue(config, pointer);
      if (!found.found) {
        if (predicate[1] === null) continue;
        return [false, false];
      }
      if (containsMarker(found.value) || typeof found.value !== typeof predicate[1] || found.value !== predicate[1]) return [false, false];
    }
  }
  if (slot.applicability !== undefined) {
    if (!Array.isArray(slot.applicability)) return [false, true];
    for (const predicate of slot.applicability) {
      if (!isRecord(predicate) || typeof predicate.path_template !== "string") return [false, true];
      const operator = predicate.operator;
      if (!["present", "nonempty", "has_string_enum", "not_false"].includes(String(operator))) return [false, true];
      const pointer = wildcardPath(predicate.path_template, actualPath).replace(/^\/config/, "");
      const found = pointerValue(config, pointer);
      if (!found.found) {
        if (operator === "not_false") continue;
        return [false, false];
      }
      if (containsMarker(found.value)) return [false, false];
      if (operator === "present" && found.value === null) return [false, false];
      if (operator === "nonempty" && !(typeof found.value === "string" && found.value.length > 0) && !(Array.isArray(found.value) && found.value.length > 0) && !(isRecord(found.value) && Object.keys(found.value).length > 0)) return [false, false];
      if (operator === "has_string_enum" && !hasStringEnum(found.value)) return [false, false];
      if (operator === "not_false" && found.value !== true && found.value !== null) return [false, false];
    }
  }
  return [true, false];
}

function declarationFitsSlot(raw: JsonValue, slot: Record<string, JsonValue>): boolean {
  if (!isRecord(raw) || typeof raw.type !== "string" || !controlTypes.has(raw.type as ControlType)) return false;
  const kind = raw.type as ControlType;
  if (!Array.isArray(slot.control_types) || !slot.control_types.includes(kind)) return false;
  const schema = isRecord(slot.expected_value_schema) ? slot.expected_value_schema : null;
  if (schema && Array.isArray(schema.types) && !schema.types.includes(kind)) return false;
  const value = raw.default;
  if (kind === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) return false;
    if (schema?.integer === true && !Number.isSafeInteger(value)) return false;
    if (typeof schema?.minimum === "number" && value < schema.minimum) return false;
    if (typeof schema?.maximum === "number" && value > schema.maximum) return false;
    return true;
  }
  if (kind === "boolean") return typeof value === "boolean";
  if (kind === "string" || kind === "enum") {
    if (typeof value !== "string" || !hasUnicodeScalars(value) || scalarLength(value) > maxStringScalars) return false;
    if (typeof schema?.min_length === "number" && scalarLength(value) < schema.min_length) return false;
    if (typeof schema?.max_length === "number" && scalarLength(value) > schema.max_length) return false;
    if (Array.isArray(schema?.allowed_values) && !schema.allowed_values.includes(value)) return false;
    return kind !== "enum" || Array.isArray(raw.allowed_values) && raw.allowed_values.includes(value);
  }
  if (!Array.isArray(value) || value.length > maxListItems || value.some((item) => typeof item !== "string" || !hasUnicodeScalars(item) || scalarLength(item) > maxStringScalars)) return false;
  if (typeof schema?.min_items === "number" && value.length < schema.min_items) return false;
  if (typeof schema?.max_items === "number" && value.length > schema.max_items) return false;
  const itemMaxLength = schema?.item_max_length;
  if (typeof itemMaxLength === "number" && value.some((item) => typeof item !== "string" || scalarLength(item) > itemMaxLength)) return false;
  const allowedValues = raw.allowed_values;
  return allowedValues == null || Array.isArray(allowedValues) && value.every((item) => allowedValues.includes(item));
}

function validateDeclaration(key: string, raw: JsonValue, diagnostics: ControlDiagnostic[]): void {
  const pointer = `/controls/${escapePointer(key)}`;
  if (!/^[A-Za-z_][A-Za-z0-9_]{0,63}$/.test(key) || forbiddenKeys.has(key) || !isRecord(raw)) {
    diagnostics.push(diag("control_declaration_invalid", pointer, "Control declaration is invalid."));
    return;
  }
  if (typeof raw.type !== "string" || !controlTypes.has(raw.type as ControlType)) {
    diagnostics.push(diag("control_declaration_invalid", `${pointer}/type`, "Control type is unsupported."));
    return;
  }
  const kind = raw.type as ControlType;
  if (!Object.hasOwn(raw, "default")) diagnostics.push(diag("control_default_required", `${pointer}/default`, "A default value is required."));
  const common = ["type", "label", "description", "default"];
  const specific: Record<ControlType, string[]> = {
    number: ["minimum", "maximum", "integer"], boolean: [], string: ["min_length", "max_length"],
    enum: ["allowed_values"], string_list: ["allowed_values", "min_items", "max_items"],
  };
  if (Object.keys(raw).some((field) => !common.includes(field) && !specific[kind]?.includes(field))) diagnostics.push(diag("control_declaration_invalid", pointer, "Declaration contains type-inapplicable fields."));
  if (typeof raw.label !== "string" || raw.label.length === 0 || scalarLength(raw.label) > 256 || !hasUnicodeScalars(raw.label)) diagnostics.push(diag("control_declaration_invalid", `${pointer}/label`, "A bounded Unicode label is required."));
  if (raw.description !== undefined && (typeof raw.description !== "string" || scalarLength(raw.description) > 1024 || !hasUnicodeScalars(raw.description))) diagnostics.push(diag("control_declaration_invalid", `${pointer}/description`, "Description must be bounded Unicode text."));
  const value = raw.default;
  if (kind === "number") {
    if (typeof value !== "number" || !Number.isFinite(value)) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default must be a finite JSON number."));
    if (typeof raw.minimum !== "number" || !Number.isFinite(raw.minimum) || typeof raw.maximum !== "number" || !Number.isFinite(raw.maximum) || raw.minimum > raw.maximum) diagnostics.push(diag("control_declaration_invalid", pointer, "Numeric controls require finite ordered bounds."));
    if (raw.integer !== undefined && typeof raw.integer !== "boolean") diagnostics.push(diag("control_declaration_invalid", `${pointer}/integer`, "Integer must be boolean."));
    if (raw.integer === true && [value, raw.minimum, raw.maximum].some((item) => typeof item !== "number" || !Number.isSafeInteger(item))) diagnostics.push(diag("control_declaration_invalid", pointer, "Integer values and bounds must be safe integers."));
    if (typeof value === "number" && Number.isFinite(value) && typeof raw.minimum === "number" && typeof raw.maximum === "number" && (value < raw.minimum || value > raw.maximum)) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default is outside its bounds."));
  } else if (kind === "boolean") {
    if (typeof value !== "boolean") diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default must be a JSON boolean."));
  } else if (kind === "string" || kind === "enum") {
    if (typeof value !== "string" || !hasUnicodeScalars(value) || scalarLength(value) > maxStringScalars) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default must be a bounded Unicode string."));
    if (kind === "string") {
      for (const field of ["min_length", "max_length"] as const) if (raw[field] != null && !boundedInteger(raw[field], maxStringScalars)) diagnostics.push(diag("control_declaration_invalid", `${pointer}/${field}`, "String bounds must be bounded nonnegative integers."));
      if (typeof value === "string" && boundedInteger(raw.min_length, maxStringScalars) && scalarLength(value) < raw.min_length) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default is shorter than its minimum length."));
      if (typeof value === "string" && boundedInteger(raw.max_length, maxStringScalars) && scalarLength(value) > raw.max_length) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default exceeds its maximum length."));
    }
    if (kind === "enum") validateAllowedValues(raw.allowed_values, pointer, diagnostics, true);
    if (kind === "enum" && typeof value === "string" && Array.isArray(raw.allowed_values) && !raw.allowed_values.includes(value)) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Enum default must be one of its allowed values."));
  } else {
    if (!Array.isArray(value) || value.length > maxListItems || value.some((item) => typeof item !== "string" || !hasUnicodeScalars(item) || scalarLength(item) > maxStringScalars)) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default must be a bounded list of strings."));
    if (raw.allowed_values != null) validateAllowedValues(raw.allowed_values, pointer, diagnostics, false);
    for (const field of ["min_items", "max_items"] as const) if (raw[field] != null && !boundedInteger(raw[field], maxListItems)) diagnostics.push(diag("control_declaration_invalid", `${pointer}/${field}`, "List bounds must be bounded nonnegative integers."));
    if (boundedInteger(raw.min_items, maxListItems) && boundedInteger(raw.max_items, maxListItems) && raw.min_items > raw.max_items) diagnostics.push(diag("control_declaration_invalid", pointer, "Minimum list size exceeds maximum."));
    if (Array.isArray(value) && boundedInteger(raw.min_items, maxListItems) && value.length < raw.min_items) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default list is shorter than its minimum."));
    if (Array.isArray(value) && boundedInteger(raw.max_items, maxListItems) && value.length > raw.max_items) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default list exceeds its maximum."));
    if (Array.isArray(value) && value.some((item) => Array.isArray(raw.allowed_values) && !raw.allowed_values.includes(item))) diagnostics.push(diag("control_value_out_of_bounds", `${pointer}/default`, "Default list contains an item outside allowed values."));
  }
}

function validateAllowedValues(value: JsonValue | undefined, pointer: string, diagnostics: ControlDiagnostic[], required: boolean): void {
  if (!Array.isArray(value) || value.length === 0 || value.length > maxListItems || value.some((item) => typeof item !== "string" || item.length === 0 || !hasUnicodeScalars(item) || scalarLength(item) > maxStringScalars) || new Set(value).size !== value.length) {
    if (required || value !== undefined) diagnostics.push(diag("control_declaration_invalid", `${pointer}/allowed_values`, "Allowed values must be bounded unique nonempty strings."));
  }
}

function pointerValue(root: unknown, pointer: string): { found: boolean; value: JsonValue | undefined } {
  let value: unknown = root;
  for (const part of pointer.split("/").slice(1).map(unescapePointer)) {
    if (isRecord(value) && Object.hasOwn(value, part)) value = value[part];
    else if (Array.isArray(value) && /^\d+$/.test(part) && Number(part) < value.length) value = value[Number(part)];
    else return { found: false, value: undefined };
  }
  return { found: true, value: value as JsonValue };
}

function wildcardPath(template: string, actual: string): string {
  const expected = template.split("/").slice(1);
  const values = actual.split("/").slice(1);
  return `/${expected.map((part, index) => part === "*" ? values[index] ?? "" : part).join("/")}`;
}

function capabilitiesShapeValid(value: unknown): value is Record<string, JsonValue> & { manifests: JsonValue[] } {
  if (!isRecord(value) || value.contract !== contract || typeof value.fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(value.fingerprint) || !Array.isArray(value.manifests) || value.manifests.length > 256) return false;
  return value.manifests.every((manifest) => {
    if (!isRecord(manifest) || typeof manifest.evaluator !== "string" || manifest.evaluator.length === 0 || typeof manifest.evaluator_version !== "string" || manifest.evaluator_version.length === 0 || typeof manifest.schema_fingerprint !== "string" || !/^[a-f0-9]{64}$/.test(manifest.schema_fingerprint) || !Array.isArray(manifest.slots) || manifest.slots.length > 512) return false;
    return manifest.slots.every((slot) => {
      if (!isRecord(slot) || slot.evaluator !== manifest.evaluator || slot.evaluator_version !== manifest.evaluator_version || slot.schema_fingerprint !== manifest.schema_fingerprint || typeof slot.path_template !== "string" || !slot.path_template.startsWith("/config/") || !Array.isArray(slot.control_types) || slot.control_types.length === 0 || slot.control_types.some((kind) => typeof kind !== "string" || !controlTypes.has(kind as ControlType)) || !["decision", "assessment"].includes(String(slot.affects)) || typeof slot.requires_server_validation !== "boolean" || !Array.isArray(slot.predicates ?? []) || !Array.isArray(slot.applicability ?? []) || !isRecord(slot.expected_value_schema)) return false;
      const slotTypes = slot.control_types;
      if (!Array.isArray(slotTypes)) return false;
      const schema = slot.expected_value_schema;
      if (!Array.isArray(schema.types) || schema.types.length !== slotTypes.length || schema.types.some((kind, index) => kind !== slotTypes[index])) return false;
      for (const key of ["minimum", "maximum"] as const) if (schema[key] != null && (typeof schema[key] !== "number" || !Number.isFinite(schema[key]))) return false;
      if (schema.integer != null && typeof schema.integer !== "boolean") return false;
      for (const [key, limit] of [["min_length", maxStringScalars], ["max_length", 16_384], ["min_items", maxListItems], ["max_items", maxListItems], ["item_max_length", maxStringScalars]] as const) if (schema[key] != null && !boundedInteger(schema[key], limit)) return false;
      if (typeof schema.minimum === "number" && typeof schema.maximum === "number" && schema.minimum > schema.maximum) return false;
      if (typeof schema.min_length === "number" && typeof schema.max_length === "number" && schema.min_length > schema.max_length) return false;
      if (typeof schema.min_items === "number" && typeof schema.max_items === "number" && schema.min_items > schema.max_items) return false;
      if (schema.allowed_values != null && (!Array.isArray(schema.allowed_values) || schema.allowed_values.some((item) => typeof item !== "string"))) return false;
      return true;
    });
  });
}

function containsMarker(root: JsonValue | undefined): boolean {
  const pending: JsonValue[] = root === undefined ? [] : [root];
  let seen = 0;
  while (pending.length > 0 && seen < maxNodes) {
    const value = pending.pop();
    seen += 1;
    if (Array.isArray(value)) pending.push(...value);
    else if (isRecord(value)) {
      if (Object.hasOwn(value, "$param")) return true;
      pending.push(...Object.values(value));
    }
  }
  return false;
}

function hasStringEnum(value: JsonValue | undefined): boolean {
  return isRecord(value) && Object.values(value).some((entry) => isRecord(entry) && Array.isArray(entry.enum) && entry.enum.length > 0 && entry.enum.every((item) => typeof item === "string"));
}

function freezeJson(value: JsonValue): Readonly<JsonObject> {
  const copy = copyCheckpointArguments([value], false)[0] as JsonValue;
  const freeze = (item: JsonValue): void => {
    if (Array.isArray(item)) item.forEach(freeze);
    else if (isRecord(item)) Object.values(item).forEach((child) => freeze(child));
    Object.freeze(item);
  };
  freeze(copy);
  return copy as Readonly<JsonObject>;
}

function isRecord(value: unknown): value is Record<string, JsonValue> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasUnicodeScalars(value: string): boolean {
  for (let index = 0; index < value.length; index += 1) {
    const unit = value.charCodeAt(index);
    if (unit >= 0xd800 && unit <= 0xdbff) {
      const next = value.charCodeAt(index + 1);
      if (!(next >= 0xdc00 && next <= 0xdfff)) return false;
      index += 1;
    } else if (unit >= 0xdc00 && unit <= 0xdfff) return false;
  }
  return true;
}

function scalarLength(value: string): number { return Array.from(value).length; }
function boundedInteger(value: unknown, max: number): value is number { return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= max; }
function escapePointer(value: string): string { return value.replace(/~/g, "~0").replace(/\//g, "~1"); }
function unescapePointer(value: string): string { return value.replace(/~1/g, "/").replace(/~0/g, "~"); }
