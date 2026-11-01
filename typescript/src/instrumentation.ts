// Modified for Norma.
import { AsyncLocalStorage } from "node:async_hooks";
import { readFile, readdir, stat } from "node:fs/promises";
import { extname, join, parse as parsePath } from "node:path";
import Ajv2020 from "ajv/dist/2020.js";
import { parseDocument } from "yaml";
import instrumentationSchema from "./instrumentation.schema.json";
import { connectionFor, NormaClient, publishAgentStepsFor, type NormaInitOptions } from "./client";
import { checkpoint, configuredSourceCheckError, isCheckpointWrapper, markCheckpointWrapper, type CheckpointOptions } from "./checkpoint";
import { copyCheckpointState } from "./checkpoint-state";
import { CheckError, FinishHeldError } from "./errors";
import { checkFinish, TurnRecorder } from "./finish";
import type { JsonObject, JsonValue, StepSchema } from "./types";
import type { DeliveryReceipt } from "./check-events";
import { resolveBinding, resolveTypeScriptV2Connection, validateTypeScriptV2Capabilities } from "./instrumentation-v2";
import { wrapConfiguredOperation, type CompiledOperationPlan, type CompiledObservation } from "./instrumentation-operations";
import { prepareOpenAIAgentAdapter, type PreparedAdapterInstall } from "./instrumentation-adapters";
import { configuredAsyncStream, type ConfiguredStreamSession, type StreamTextSelection } from "./instrumentation-streams";
import { prepareInstrumentationInputSchemas, validateCapturedInput } from "./instrumentation-input-schema";
import { selectorRootValue, validateAllParamsParameterNames, validateParameterSelector, type SelectorValueRoot } from "./instrumentation-selectors";
import { normalizeControlContract } from "./control-contract";

export type BindingReference = { binding: string };
export type EnvironmentReference = { env: string };
export type ValueRoot = SelectorValueRoot;
export type ValueSource =
  | { value: JsonValue }
  | { path: [ValueRoot, ...(string | number)[]] }
  | { provider: string };
export type ConfiguredActionValue = string | null | ValueSource;
export interface InstrumentationAction {
  kind: "shell" | "edit" | "read" | "search" | "web" | "mcp" | "task" | "other";
  target?: ConfiguredActionValue;
  command?: ConfiguredActionValue;
  cwd?: ConfiguredActionValue;
}
export interface InstrumentationNativeToolAction {
  kind: InstrumentationAction["kind"];
  target?: ConfiguredActionValue;
  command?: ConfiguredActionValue;
}
export interface InlineCheckpointOptions {
  client?: BindingReference;
  stepName?: string;
  stepType?: string;
  targetType?: string;
  targetId?: string;
  action?: InstrumentationAction | BindingReference;
  state?: JsonObject | BindingReference;
  action_kind?: InstrumentationAction["kind"];
  action_target_arg?: string;
  action_command_arg?: string;
  action_cwd_arg?: string;
}
export interface InstrumentationCheckpoint {
  target: string;
  parameter_names?: string[];
  step_type?: string;
  state?: JsonObject;
  state_provider?: string;
  input_schema?: JsonObject;
  options?: string | InlineCheckpointOptions;
  action?: InstrumentationAction;
  state_from?: ValueSource;
  input_from?: ValueSource | "all_kwargs" | "all_params";
  output_from?: ValueSource;
  context_from?: ValueSource;
  tools_from?: ValueSource;
  ground_truth_from?: ValueSource;
}
export interface InstrumentationEntrypoint {
  target: string;
  parameter_names?: string[];
  /** Declare this when a non-async function returns a Promise or AsyncIterable. */
  returns?: "promise" | "async_iterable";
  context?: ValueSource;
  target_scope?: { type: ValueSource; id: ValueSource };
  trace?: { trace_id: ValueSource; span_id: ValueSource };
  turn?: InstrumentationTurn;
}
export interface InstrumentationConnection {
  runtime_url?: string | EnvironmentReference;
  agent_key?: EnvironmentReference | BindingReference;
  transport?: BindingReference;
  http_client?: BindingReference;
  debug_logger?: BindingReference;
  user_agent?: string;
  timeout_ms?: number;
  target_type?: string;
  target_id?: string;
  control_contract?: "rule-controls" | "rule-controls-v1";
}
export interface InstrumentationTelemetry {
  enabled?: boolean;
  sink_name?: string;
  sink_config?: JsonObject;
  batch_size?: number;
  flush_interval?: number;
  max_retries?: number;
  retry_delay?: number;
  shutdown_join_timeout?: number;
  shutdown_flush_timeout?: number;
  shutdown_max_failed_flushes?: number;
  otel_enabled?: boolean;
  otel_endpoint?: string | EnvironmentReference;
  otel_headers?: Record<string, string | EnvironmentReference>;
  otel_service_name?: string;
  otel_tracer_provider?: BindingReference;
  logging?: {
    config?: BindingReference;
    log_enabled?: boolean;
    log_span_start?: boolean;
    log_span_end?: boolean;
    log_rule_eval?: boolean;
    log_span_results?: boolean;
  };
}
export interface InstrumentationSchemaOptions { publish?: boolean; steps?: Array<{
  type: string; name: string; description?: string | null; input_schema?: JsonObject | null;
  output_schema?: JsonObject | null; metadata?: JsonObject | null;
}> }
export interface InstrumentationTurnFinish {
  driver?: "generic";
  final_message: ValueSource;
  on_hold: "raise" | "return";
  continue_with?: BindingReference;
  on_verdict?: BindingReference;
}
export interface InstrumentationStreamFinish {
  text?: ValueSource;
  mode?: "concat" | "last";
  max_chars?: number;
  missing?: "error" | "skip";
}
export interface InstrumentationStreamTurnFinish {
  driver?: "generic";
  stream: InstrumentationStreamFinish;
  on_hold: "raise" | "return";
  on_verdict?: BindingReference;
}
export interface InstrumentationNativeTurnFinish {
  driver: "openai_agents" | "pydantic_ai"; adapter: string; on_verdict?: BindingReference;
}
export interface InstrumentationTurn {
  session_id: ValueSource; turn_id: ValueSource; user_request?: ValueSource;
  platform?: string; platform_version?: string; application?: string; application_version?: string;
  model?: string; max_continuations?: number;
  finish?: InstrumentationTurnFinish | InstrumentationStreamTurnFinish | InstrumentationNativeTurnFinish;
}
export interface InstrumentationOperationObservation {
  event_key: ValueSource; summary: ValueSource; external_reference?: ValueSource;
}
export interface InstrumentationOperation {
  target: string; parameter_names?: string[]; returns?: "promise"; operation_key: ValueSource; input?: ValueSource; context?: ValueSource; evidence?: ValueSource;
  tools?: ValueSource; action?: InstrumentationNativeToolAction; unresolved_policy?: "block" | "allow";
  observations?: { attempted?: InstrumentationOperationObservation; succeeded?: InstrumentationOperationObservation; failed?: InstrumentationOperationObservation };
  verification_session_id?: ValueSource; on_record?: BindingReference;
}
export interface InstrumentationOpenAIToolDefinition {
  description: string; parameters: JsonObject; execute: BindingReference; needs_approval?: boolean;
  input_guardrails?: BindingReference[]; output_guardrails?: BindingReference[]; on_record?: BindingReference;
  action?: InstrumentationNativeToolAction;
}
export interface InstrumentationOpenAINativeTool { tool: BindingReference; check_output?: boolean; context_extractor?: BindingReference; action?: InstrumentationNativeToolAction }
export type InstrumentationAdapter =
  | { kind: "openai_agents"; agent: BindingReference; runner?: BindingReference; tools?: Record<string, InstrumentationOpenAIToolDefinition | InstrumentationOpenAINativeTool> }
  | { kind: "google_adk"; runner: BindingReference; agent?: BindingReference; enabled_hooks?: string[]; blocked_message_template?: string;
      step_name_overrides?: Record<string, string>; step_name_resolver?: BindingReference; context_extractor?: BindingReference;
      on_violation_callback?: BindingReference; enable_logging?: boolean }
  | { kind: "strands"; agent: BindingReference; event_types?: string[]; on_violation_callback?: BindingReference; enable_logging?: boolean }
  | { kind: "pydantic_ai"; agent: BindingReference };
export interface InstrumentationConfigV1 {
  version: 1;
  entrypoints: string[];
  checkpoints: Record<string, InstrumentationCheckpoint>;
}
export interface InstrumentationConfigV2 {
  version: 2;
  entrypoints: Array<string | InstrumentationEntrypoint>;
  checkpoints: Record<string, InstrumentationCheckpoint>;
  connection?: InstrumentationConnection;
  telemetry?: InstrumentationTelemetry;
  refresh?: { interval_seconds?: number };
  schemas?: InstrumentationSchemaOptions;
  operations?: Record<string, InstrumentationOperation>;
  adapters?: Record<string, InstrumentationAdapter>;
}
export type InstrumentationConfig = InstrumentationConfigV1 | InstrumentationConfigV2;
export type StateProvider = (...args: unknown[]) => JsonObject | Promise<JsonObject>;
type AgentConnection = Omit<NormaInitOptions, "agentName">;
type AgentConnections = AgentConnection | NormaClient;
type NativeOptionRegistry = Record<string, CheckpointOptions<unknown[]>>;
export interface ConfigureOptions {
  /** Required for in-memory configuration; file names supply identity otherwise. */
  agentName?: string;
  /** Selected checkpoint targets must return Promise results; callers await those slots. */
  bindings: Record<string, object>;
  providers?: Record<string, StateProvider>;
  /** Flat registry of named native CheckpointOptions referenced by config entries. */
  options?: NativeOptionRegistry;
  connection?: AgentConnection;
  connections?: Record<string, AgentConnections>;
}

const instrumentationAjv = new Ajv2020({ allErrors: true, strict: true, strictRequired: false, strictTuples: false });
// Ajv's default uniqueItems comparator calls valueOf() on null-prototype clones.
instrumentationAjv.removeKeyword("uniqueItems");
instrumentationAjv.addKeyword({
  keyword: "uniqueItems",
  type: "array",
  schemaType: "boolean",
  errors: false,
  validate: (enabled: boolean, value: unknown): boolean => {
    if (!enabled || !Array.isArray(value)) return true;
    for (let index = 0; index < value.length; index += 1) {
      for (let other = index + 1; other < value.length; other += 1) {
        if (sameJson(value[index], value[other])) return false;
      }
    }
    return true;
  },
});
const validator = instrumentationAjv.compile<InstrumentationConfig>(instrumentationSchema);
const version1Validator = instrumentationAjv.compile({ $ref: `${instrumentationSchema.$id}#/$defs/version1` });
const version2Validator = instrumentationAjv.compile({ $ref: `${instrumentationSchema.$id}#/$defs/version2` });
interface AgentScope { token: symbol; agentName: string; revision: number; context: JsonObject; targetType?: string; targetId?: string; traceId?: string; spanId?: string }
const agentContext = new AsyncLocalStorage<AgentScope>();
function contextFor(agentName: string): JsonObject {
  const scope = agentContext.getStore();
  return scope?.agentName === agentName ? scope.context : {};
}
const pathPattern = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/;
const asyncFunctionPrototype = Object.getPrototypeOf(async function () {});
const generatorFunctionPrototype = Object.getPrototypeOf(function* () {});
const asyncGeneratorFunctionPrototype = Object.getPrototypeOf(async function* () {});
function isNativeAsyncFunction(value: (...args: never[]) => unknown): boolean { return Object.getPrototypeOf(value) === asyncFunctionPrototype; }
function isNativeAsyncGeneratorFunction(value: (...args: never[]) => unknown): boolean { return Object.getPrototypeOf(value) === asyncGeneratorFunctionPrototype; }
function requirePromiseDeclaration(value: (...args: never[]) => unknown, declared: "promise" | undefined, label: string): void {
  if (!isNativeAsyncFunction(value) && declared !== "promise") {
    throw new TypeError(`${label} must be an async function or declare returns: "promise".`);
  }
}
function requirePromiseResult(value: unknown, label: string): asserts value is PromiseLike<unknown> {
  if (value === null || (typeof value !== "object" && typeof value !== "function")
      || typeof (value as { then?: unknown }).then !== "function") {
    throw new TypeError(`${label} declared returns: "promise" but returned a non-Promise value.`);
  }
}
function asyncIteratorFrom(value: unknown): AsyncIterator<unknown> {
  if ((typeof value !== "object" && typeof value !== "function") || value === null) {
    throw new TypeError("Stream entrypoint must return an AsyncIterable.");
  }
  const factory = (value as { [Symbol.asyncIterator]?: unknown })[Symbol.asyncIterator];
  if (typeof factory !== "function") throw new TypeError("Stream entrypoint must return an AsyncIterable with Symbol.asyncIterator.");
  const iterator: unknown = Reflect.apply(factory, value, []);
  if ((typeof iterator !== "object" && typeof iterator !== "function") || iterator === null
      || typeof (iterator as { next?: unknown }).next !== "function") {
    throw new TypeError("Stream entrypoint Symbol.asyncIterator must return an iterator with next().");
  }
  return iterator as AsyncIterator<unknown>;
}
const agentNamePattern = /^[a-z0-9:_-]{10,}$/;
const nativeOptionKeys = new Set(["client", "stepName", "stepType", "targetType", "targetId", "action", "state"]);
const connectionOptionKeys = new Set(["targetType", "targetId", "runtimeUrl", "agentKey", "timeoutMs", "observabilityEnabled", "controlContract", "userAgent", "debugLogger", "httpClient"]);
const actionKinds = new Set(["shell", "edit", "read", "search", "web", "mcp", "task", "other"]);

function cloneJson(value: unknown, active = new Set<object>()): JsonValue {
  if (value === null || typeof value === "string" || typeof value === "boolean") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value) || (Number.isInteger(value) && !Number.isSafeInteger(value)) || Object.is(value, -0)) {
      throw new TypeError("Instrumentation JSON must contain finite numbers and safe integers.");
    }
    return value;
  }
  if (typeof value !== "object" || active.has(value)) throw new TypeError("Instrumentation values must be plain JSON without cycles.");
  const array = Array.isArray(value);
  const prototype = Object.getPrototypeOf(value);
  if (array ? prototype !== Array.prototype : prototype !== Object.prototype && prototype !== null) {
    throw new TypeError("Instrumentation values must be plain JSON objects and arrays.");
  }
  active.add(value);
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.some(key => typeof key !== "string")) throw new TypeError("Instrumentation JSON cannot contain symbol keys.");
  const names = (keys as string[]).filter(key => !(array && key === "length"));
  if (array && (descriptors.length?.value !== names.length || names.some((key, index) => key !== String(index)))) {
    throw new TypeError("Instrumentation arrays must not contain holes or extra properties.");
  }
  const result: JsonValue[] | JsonObject = array ? [] : Object.create(null) as JsonObject;
  for (const key of names) {
    const descriptor = descriptors[key];
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) {
      throw new TypeError("Instrumentation JSON cannot contain accessors or non-enumerable properties.");
    }
    if (!array) Object.defineProperty(result, key, { value: cloneJson(descriptor.value, active), enumerable: true, configurable: true, writable: true });
    else (result as JsonValue[]).push(cloneJson(descriptor.value, active));
  }
  active.delete(value);
  return result;
}

export class InstrumentationValidationError extends TypeError {
  public readonly code: "schema" | "capability" | "selector" | "identity";
  public readonly path: string;
  public readonly line: number | null;
  public readonly column: number | null;
  public readonly issues: readonly { code: "schema" | "capability" | "selector" | "identity"; path: string; message: string }[];

  public constructor(code: InstrumentationValidationError["code"], message: string, path = "", line: number | null = null, column: number | null = null, issues?: readonly { code: "schema" | "capability" | "selector" | "identity"; path: string; message: string }[]) {
    super(message);
    this.name = "InstrumentationValidationError";
    this.code = code;
    this.path = path;
    this.line = line;
    this.column = column;
    this.issues = issues ?? [{ code, path, message }];
  }
}

export class InstrumentationSyntaxError extends SyntaxError {
  public readonly code = "syntax" as const;
  public readonly path: string;
  public readonly line: number | null;
  public readonly column: number | null;

  public constructor(message: string, path = "", line: number | null = null, column: number | null = null) {
    super(message);
    this.name = "SyntaxError";
    this.path = path;
    this.line = line;
    this.column = column;
  }
}

function schemaFailure(value: unknown): never {
  const errors = validator.errors ?? [];
  const selectedValidator = value && typeof value === "object" && !Array.isArray(value) && (value as { version?: unknown }).version === 2 ? version2Validator : version1Validator;
  selectedValidator(value);
  const branchErrors = selectedValidator.errors ?? [];
  const useful = (branchErrors.length ? branchErrors : errors).filter(error => !["oneOf", "anyOf", "if", "then", "else"].includes(error.keyword));
  const selected = useful[0] ?? errors[0];
  const candidates = useful.length ? useful : errors;
  const alternativePaths = new Set(errors.filter(error => error.keyword === "oneOf").map(error => error.instancePath ?? ""));
  const fieldPath = (error: NonNullable<typeof errors>[number]): string => {
    const instancePath = error.instancePath ?? "";
    const property = error.keyword === "additionalProperties" ? (error.params as { additionalProperty?: unknown }).additionalProperty : undefined;
    const missing = error.keyword === "required" ? (error.params as { missingProperty?: unknown }).missingProperty : undefined;
    const suffix = typeof property === "string" ? property : typeof missing === "string" ? missing : undefined;
    return typeof suffix === "string" ? `${instancePath}/${suffix.replace(/~/g, "~0").replace(/\//g, "~1")}` : instancePath;
  };
  const pruned = candidates.filter(error => {
    if (error.keyword === "required" && alternativePaths.has(error.instancePath ?? "")
        && candidates.some(other => other.keyword === "additionalProperties" && (other.instancePath ?? "") === (error.instancePath ?? ""))) return false;
    if (error.keyword === "type" && candidates.some(other => other !== error && fieldPath(other).startsWith(`${error.instancePath ?? ""}/`))) return false;
    return true;
  });
  const unique = new Map<string, { code: "schema"; path: string; message: string }>();
  for (const error of pruned) {
    const path = fieldPath(error);
    const message = error.keyword === "additionalProperties" ? "Unknown configuration field." : error.keyword === "required" ? "Required configuration field is missing." : error.keyword === "uniqueItems" ? "uniqueItems validation failed." : error.keyword === "type" ? "Configuration field has an invalid type." : error.keyword === "enum" || error.keyword === "const" ? "Configuration field has an unsupported value." : "Configuration field does not match the instrumentation schema.";
    unique.set(`${path}:${error.keyword}`, { code: "schema", path, message });
  }
  const issues = [...unique.values()];
  const path = issues[0]?.path ?? selected?.instancePath ?? "";
  throw new InstrumentationValidationError("schema", `Invalid instrumentation configuration: ${issues.map(item => item.message).join("; ")}`, path, null, null, issues);
}

function syntaxFailure(format: "json" | "yaml", text: string, error?: Error & { linePos?: Array<{ line: number; col: number }> }): never {
  const linePos = error?.linePos?.[0];
  if (linePos) throw new InstrumentationSyntaxError(`Invalid ${format.toUpperCase()} instrumentation text.`, "", linePos.line, linePos.col);
  if (format === "json") {
    const match = /position\s+(\d+)/i.exec(error?.message ?? "");
    if (match) {
      const offset = Number(match[1]);
      const before = text.slice(0, offset);
      const lines = before.split(/\r\n|\r|\n/);
      throw new InstrumentationSyntaxError("Invalid JSON instrumentation text.", "", lines.length, (lines.at(-1)?.length ?? 0) + 1);
    }
  }
  throw new InstrumentationSyntaxError(`Invalid ${format.toUpperCase()} instrumentation text.`);
}

function rejectYamlTags(node: unknown, seen = new Set<object>()): void {
  if (!node || typeof node !== "object" || seen.has(node)) return;
  seen.add(node);
  const record = node as { tag?: unknown; items?: unknown; key?: unknown; value?: unknown };
  if (record.tag === "tag:yaml.org,2002:merge") {
    throw new SyntaxError("YAML merge tags are not supported in instrumentation config.");
  }
  if (typeof record.tag === "string" && record.tag.startsWith("!") && !record.tag.startsWith("tag:yaml.org,2002:")) {
    throw new SyntaxError("Custom YAML tags are not supported in instrumentation config.");
  }
  if (Array.isArray(record.items)) {
    for (const item of record.items) rejectYamlTags(item, seen);
  }
  if ("key" in record) rejectYamlTags(record.key, seen);
  if ("value" in record) rejectYamlTags(record.value, seen);
}

function validateConfig(value: unknown): InstrumentationConfig {
  const copy = cloneJson(value);
  if (!validator(copy)) schemaFailure(copy);
  const config = copy as unknown as InstrumentationConfig;
  if (config.version === 2) {
    for (const configuredEntrypoint of config.entrypoints) {
      if (typeof configuredEntrypoint === "string" || !configuredEntrypoint.turn) continue;
      const turn = configuredEntrypoint.turn;
      for (const [alias, canonical, label] of [
        ["application", "platform", "application/platform"],
        ["application_version", "platform_version", "application_version/platform_version"],
      ] as const) {
        const aliasValue = turn[alias];
        const canonicalValue = turn[canonical];
        if (aliasValue !== undefined && canonicalValue !== undefined && aliasValue !== canonicalValue) {
          throw new TypeError(`turn.${label} values must match when both are supplied.`);
        }
        if (aliasValue !== undefined) {
          turn[canonical] = aliasValue;
          delete turn[alias];
        }
      }
    }
    if (config.connection?.control_contract === "rule-controls") config.connection.control_contract = "rule-controls-v1";
  }
  for (const [name, item] of Object.entries(config.checkpoints)) {
    if (item.state !== undefined) item.state = copyCheckpointState(item.state);
    if (item.step_type === undefined) item.step_type = "tool";
    if (typeof item.step_type !== "string" || !item.step_type.trim()) throw new TypeError("step_type must be a nonblank string.");
    if (!name.trim()) throw new TypeError("Checkpoint names must be nonblank.");
  }
  prepareInstrumentationInputSchemas(config);
  return config;
}

/** Parse and validate declarative JSON or YAML without reading files or accessing the network. */
export function parseInstrumentation(text: string, format: "json" | "yaml" = "json"): InstrumentationConfig {
  if (typeof text !== "string") throw new TypeError("Instrumentation input must be text.");
  let value: unknown;
  if (format === "json") {
    const document = parseDocument(text, { uniqueKeys: true, schema: "json" });
    if (document.errors.length) syntaxFailure("json", text, document.errors[0]);
    try { value = JSON.parse(text) as unknown; } catch (error) { syntaxFailure("json", text, error as Error); }
  } else if (format === "yaml") {
    const document = parseDocument(text, { uniqueKeys: true, schema: "core" });
    if (document.errors.length) syntaxFailure("yaml", text, document.errors[0]);
    const yamlVersion = document.directives.yaml;
    if (yamlVersion.explicit && yamlVersion.version !== "1.2") {
      throw new InstrumentationSyntaxError("Instrumentation YAML must use YAML 1.2.");
    }
    rejectYamlTags(document.contents);
    try { value = document.toJS({ maxAliasCount: 50 }) as unknown; } catch (error) { syntaxFailure("yaml", text, error as Error); }
  } else {
    throw new TypeError("Instrumentation format must be 'json' or 'yaml'.");
  }
  return validateConfig(value);
}

interface Slot {
  owner: object;
  key: string;
  descriptor: PropertyDescriptor;
  original?: PropertyDescriptor;
  value: (...args: never[]) => unknown;
  path: string;
}
interface CheckpointPlan extends Slot {
  agentName: string;
  stepName: string;
  stepType: string;
  options: CheckpointOptions<unknown[]>;
}
interface OperationPlan extends Slot {
  agentName: string;
  plan: CompiledOperationPlan;
}
interface EntrypointPlan extends Slot {
  agentName: string;
  context?: ValueResolver;
  targetType?: ValueResolver;
  targetId?: ValueResolver;
  traceId?: ValueResolver;
  spanId?: ValueResolver;
  turn?: PreparedTurn;
  streamReturn?: "promise" | "async_iterable";
}
interface PreparedStreamFinish {
  mode: "concat" | "last";
  maxChars: number;
  missing: "error" | "skip";
  selectText: (invocation: InvocationValues) => StreamTextSelection | Promise<StreamTextSelection>;
}
interface PreparedTurn {
  config: InstrumentationTurn;
  sessionId: ValueResolver;
  turnId: ValueResolver;
  userRequest?: ValueResolver;
  finalMessage?: ValueResolver;
  stream?: PreparedStreamFinish;
  continueWith?: { owner: object; callback: (...args: unknown[]) => unknown };
  onVerdict?: { owner: object; callback: (...args: unknown[]) => unknown };
}
interface InstalledSlot { owner: object; key: string; original?: PropertyDescriptor; wrapper: (...args: never[]) => unknown }

function dataDescriptor(owner: object, key: string): { owner: object; descriptor: PropertyDescriptor } | undefined {
  let current: object | null = owner;
  while (current !== null) {
    const descriptor = Object.getOwnPropertyDescriptor(current, key);
    if (descriptor) return { owner: current, descriptor };
    current = Object.getPrototypeOf(current) as object | null;
  }
  return undefined;
}

function resolveTarget(bindings: Record<string, object>, path: string, allowAsyncGenerator = false): Slot {
  if (!pathPattern.test(path)) throw new TypeError(`Invalid instrumentation target path: ${path}`);
  const segments = path.split(".");
  const rootDescriptor = Object.getOwnPropertyDescriptor(bindings, segments[0]!);
  if (!rootDescriptor || !Object.hasOwn(rootDescriptor, "value")) {
    throw new TypeError(`Binding root '${segments[0]}' must be an own data property.`);
  }
  let current: unknown = rootDescriptor.value;
  if ((typeof current !== "object" && typeof current !== "function") || current === null) {
    throw new TypeError(`Binding root '${segments[0]}' must be an object.`);
  }
  let selected: { owner: object; descriptor: PropertyDescriptor } | undefined;
  let receiver: object = current as object;
  for (let index = 1; index < segments.length; index++) {
    const segment = segments[index]!;
    receiver = current as object;
    selected = dataDescriptor(current as object, segment);
    if (!selected || !Object.hasOwn(selected.descriptor, "value")) {
      throw new TypeError(`Instrumentation target '${path}' must resolve through data properties only.`);
    }
    current = selected.descriptor.value;
    if (index < segments.length - 1 && ((typeof current !== "object" && typeof current !== "function") || current === null)) {
      throw new TypeError(`Instrumentation target '${path}' has a non-object path segment.`);
    }
  }
  if (!selected || typeof current !== "function") throw new TypeError(`Instrumentation target '${path}' must be a function.`);
  const inheritedDescriptor = selected.descriptor;
  if (inheritedDescriptor.writable !== true) throw new TypeError(`Instrumentation target '${path}' is not writable.`);
  const original = Object.getOwnPropertyDescriptor(receiver, segments.at(-1)!);
  if (!original && !Object.isExtensible(receiver)) throw new TypeError(`Inherited instrumentation target '${path}' cannot be shadowed on the bound object.`);
  const descriptor = original ?? { ...inheritedDescriptor, configurable: true };
  if (isCheckpointWrapper(current)) throw new TypeError(`Instrumentation target '${path}' is already a Norma checkpoint wrapper.`);
  const functionPrototype = Object.getPrototypeOf(current);
  if (functionPrototype === generatorFunctionPrototype || (functionPrototype === asyncGeneratorFunctionPrototype && !allowAsyncGenerator)) {
    throw new TypeError(`Instrumentation target '${path}' cannot be a generator function.`);
  }
  return { owner: receiver, key: segments.at(-1)!, descriptor, original, value: current as (...args: never[]) => unknown, path };
}

function slotId(slot: Slot): string { return `${objectId(slot.owner)}:${slot.key}`; }
const objectIds = new WeakMap<object, number>();
let nextObjectId = 1;
function objectId(value: object): number {
  let id = objectIds.get(value);
  if (id === undefined) { id = nextObjectId++; objectIds.set(value, id); }
  return id;
}

function validateAgentName(name: string): void {
  if (!agentNamePattern.test(name)) throw new TypeError("Agent names must be exact lowercase registered names using letters, digits, colon, underscore or hyphen (at least 10 characters).");
}

export function assertInstrumentationAgentName(name: string): void { validateAgentName(name); }

function snapshotAction(action: CheckpointOptions<unknown[]>["action"]): CheckpointOptions<unknown[]>["action"] {
  if (typeof action === "function" || action === undefined) return action;
  if (!action || typeof action !== "object" || Object.getPrototypeOf(action) !== Object.prototype) {
    throw new TypeError("Checkpoint action options must use kind, target, command and cwd only.");
  }
  const descriptors = Object.getOwnPropertyDescriptors(action);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.some(key => typeof key !== "string" || !["kind", "target", "command", "cwd"].includes(key as string))) {
    throw new TypeError("Checkpoint action options must use kind, target, command and cwd only.");
  }
  const copy: Record<string, unknown> = {};
  for (const key of keys as string[]) {
    const descriptor = descriptors[key]!;
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError("Checkpoint action options cannot contain accessors.");
    copy[key] = descriptor.value;
  }
  if (!actionKinds.has(copy.kind as string) || [copy.target, copy.command, copy.cwd].some(value => value !== undefined && value !== null && typeof value !== "string")) {
    throw new TypeError("Checkpoint action options contain an invalid static value.");
  }
  return Object.freeze(copy) as unknown as CheckpointOptions<unknown[]>["action"];
}
function snapshotNativeOptions(value: unknown): CheckpointOptions<unknown[]> {
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) throw new TypeError("Named checkpoint options must be plain objects.");
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.some(key => typeof key !== "string" || !nativeOptionKeys.has(key as string))) throw new TypeError("Unknown native checkpoint option.");
  const copied: Record<string, unknown> = {};
  for (const key of keys as string[]) {
    const descriptor = descriptors[key]!;
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError("Named checkpoint options cannot contain accessors.");
    copied[key] = descriptor.value;
  }
  const options = copied as CheckpointOptions<unknown[]>;
  if (options.stepType !== undefined && (typeof options.stepType !== "string" || !options.stepType.trim())) throw new TypeError("stepType must be nonblank.");
  if (options.stepName !== undefined && (typeof options.stepName !== "string" || !options.stepName.trim())) throw new TypeError("stepName must be nonblank.");
  if ([options.targetType, options.targetId].some(item => item !== undefined && (typeof item !== "string" || !item.trim()))) throw new TypeError("targetType and targetId must be nonblank strings.");
  options.action = snapshotAction(options.action);
  if (options.state !== undefined && typeof options.state !== "function") options.state = copyCheckpointState(options.state);
  if (options.state !== undefined && typeof options.state !== "function" && (!options.state || typeof options.state !== "object" || Array.isArray(options.state))) {
    throw new TypeError("Checkpoint state must be a JSON object or callback.");
  }
  return options;
}
function resolveInlineNativeOptions(
  value: InlineCheckpointOptions,
  bindings: Record<string, object>,
  agentName: string,
  parameterNames?: readonly string[],
  stepName?: string,
): CheckpointOptions<unknown[]> {
  const copied: Record<string, unknown> = { ...value };
  const clientRef = copied.client;
  if (clientRef && typeof clientRef === "object") {
    const client = resolveBinding(bindings, (clientRef as BindingReference).binding).value;
    if (!(client instanceof NormaClient)) throw new TypeError("Inline options client binding must resolve to a NormaClient.");
    copied.client = client;
  }
  const action = copied.action;
  if (action && typeof action === "object" && !Array.isArray(action)) {
    if (Object.keys(action).length === 1 && typeof (action as { binding?: unknown }).binding === "string") {
      const resolved = resolveBinding(bindings, (action as BindingReference).binding);
      if (typeof resolved.value !== "function") throw new TypeError("Inline options action binding must resolve to a callback.");
      copied.action = resolved.value.bind(resolved.owner);
    } else {
      copied.action = actionResolver(action as InstrumentationAction, bindings, agentName, parameterNames,
        { label: `Checkpoint '${stepName ?? "unknown"}'`, pointer: `/checkpoints/${pointerSegment(stepName ?? "unknown")}/options/action` });
    }
  }
  const state = copied.state;
  if (state && typeof state === "object" && !Array.isArray(state)
      && Object.keys(state).length === 1 && typeof (state as { binding?: unknown }).binding === "string") {
    const resolved = resolveBinding(bindings, (state as BindingReference).binding);
    if (typeof resolved.value !== "function") throw new TypeError("Inline options state binding must resolve to a callback.");
    copied.state = resolved.value.bind(resolved.owner);
  }
  return snapshotNativeOptions(copied);
}

interface InvocationValues { args: unknown[]; kwargs: Record<string, never>; result?: unknown; error?: unknown; context: JsonObject; agent_name: string }
type ValueResolver = (invocation: InvocationValues) => JsonValue | Promise<JsonValue>;
function pointerSegment(value: string): string { return value.replace(/~/g, "~0").replace(/\//g, "~1"); }
function checkpointSourceResolver(
  source: ValueSource,
  bindings: Record<string, object>,
  phase: "pre" | "post",
  parameterNames: readonly string[] | undefined,
  stepName: string,
  field: string,
): ValueResolver {
  const pointer = `/checkpoints/${pointerSegment(stepName)}/${field}`;
  if ("path" in source) validateParameterSelector(source.path, parameterNames, "Value source");
  let resolver: ValueResolver;
  try {
    resolver = valueResolver(source, bindings, phase, parameterNames);
  } catch {
    const expected = "provider" in source
      ? `a callback in ConfigureOptions.bindings at '${source.provider}'`
      : "a valid JSON value source";
    throw new TypeError(`Checkpoint '${stepName}' source '${pointer}' must resolve from ${expected}.`);
  }
  return async invocation => {
    try {
      return await resolver(invocation);
    } catch {
      throw configuredSourceCheckError(phase,
        `Checkpoint '${stepName}' source '${pointer}' failed during ${phase}; the configured path is missing or invalid, or its provider did not return a JSON value.`);
    }
  };
}
function valueResolver(
  source: ValueSource,
  bindings: Record<string, object>,
  phase: "pre" | "post",
  parameterNames?: readonly string[],
): ValueResolver {
  if ("value" in source) {
    const literal = cloneJson(source.value);
    return () => cloneJson(literal);
  }
  if ("provider" in source) {
    const resolved = resolveBinding(bindings, source.provider);
    if (typeof resolved.value !== "function") throw new TypeError(`Provider '${source.provider}' must bind to a callback.`);
    const provider = resolved.value as (...args: unknown[]) => unknown;
    return invocation => {
      const value = provider.call(resolved.owner, invocation);
      return value instanceof Promise ? value.then(cloneJson) : cloneJson(value);
    };
  }
  const [root, ...segments] = source.path;
  if (phase === "pre" && (root === "result" || root === "error")) {
    throw new TypeError(`Value source rooted at '${root}' cannot be used before the action returns.`);
  }
  validateParameterSelector(source.path, parameterNames, "Value source");
  return invocation => {
    let current: unknown = selectorRootValue(root, invocation, parameterNames);
    for (const segment of segments) {
      if (current === null || (typeof current !== "object" && typeof current !== "function")) throw new TypeError("Value source path does not exist at runtime.");
      const key = String(segment);
      const descriptor = Object.getOwnPropertyDescriptor(current, key);
      if (!descriptor || !Object.hasOwn(descriptor, "value")) throw new TypeError("Value source path is missing or resolves through an accessor.");
      current = descriptor.value;
    }
    return cloneJson(current);
  };
}

function streamTextResolver(
  source: ValueSource | undefined,
  bindings: Record<string, object>,
  parameterNames?: readonly string[],
): PreparedStreamFinish["selectText"] {
  if (source === undefined) return invocation => ({ present: true, value: invocation.result });
  if ("value" in source) {
    const literal = cloneJson(source.value);
    return () => ({ present: true, value: literal });
  }
  if ("provider" in source) {
    const resolved = resolveBinding(bindings, source.provider);
    if (typeof resolved.value !== "function") throw new TypeError(`Provider '${source.provider}' must bind to a callback.`);
    const provider = resolved.value as (...args: unknown[]) => unknown;
    return invocation => {
      const value = provider.call(resolved.owner, invocation);
      return value instanceof Promise ? value.then(result => ({ present: true, value: result })) : { present: true, value };
    };
  }
  const [root, ...segments] = source.path;
  validateParameterSelector(source.path, parameterNames, "Stream text selector");
  return invocation => {
    let current: unknown = selectorRootValue(root, invocation, parameterNames);
    for (const segment of segments) {
      if (current === null || (typeof current !== "object" && typeof current !== "function")) return { present: false };
      const descriptor = Object.getOwnPropertyDescriptor(current, String(segment));
      if (!descriptor) return { present: false };
      if (!Object.hasOwn(descriptor, "value")) throw new TypeError("Stream text selector path cannot resolve through an accessor.");
      current = descriptor.value;
    }
    return { present: true, value: current };
  };
}

function actionResolver(
  action: InstrumentationAction,
  bindings: Record<string, object>,
  agentName: string,
  parameterNames: readonly string[] | undefined,
  sourceInfo: { label: string; pointer: string },
): NonNullable<CheckpointOptions<unknown[]>["action"]> {
  const values = Object.fromEntries((["target", "command", "cwd"] as const).map(key => {
    const source = action[key];
    if (source === undefined || source === null || typeof source === "string") return [key, async () => source];
    const pointer = `${sourceInfo.pointer}/${key}`;
    let resolver: ValueResolver;
    try { resolver = valueResolver(source, bindings, "pre", parameterNames); }
    catch {
      const expected = "provider" in source
        ? `a callback in ConfigureOptions.bindings at '${source.provider}'`
        : "a valid JSON value source";
      throw new TypeError(`${sourceInfo.label} source '${pointer}' must resolve from ${expected}.`);
    }
    return [key, async (invocation: InvocationValues) => {
      try { return await resolver(invocation); }
      catch {
        throw configuredSourceCheckError("pre", `${sourceInfo.label} source '${pointer}' failed during pre; verify the configured source and its JSON value.`);
      }
    }];
  })) as Record<"target" | "command" | "cwd", (invocation: InvocationValues) => Promise<string | null | JsonValue | undefined>>;
  return async (...args: unknown[]) => {
    const invocation = { args, kwargs: {}, context: contextFor(agentName), agent_name: agentName } as InvocationValues;
    const [target, command, cwd] = await Promise.all([values.target(invocation), values.command(invocation), values.cwd(invocation)]);
    for (const [key, value] of [["target", target], ["command", command], ["cwd", cwd]] as const) {
      if (value !== undefined && value !== null && typeof value !== "string") {
        throw configuredSourceCheckError("pre", `${sourceInfo.label} source '${sourceInfo.pointer}/${key}' must resolve to a string or null.`);
      }
    }
    return { kind: action.kind, target: target as string | null | undefined, command: command as string | null | undefined, cwd: cwd as string | null | undefined };
  };
}
function optionsFor(registry: NativeOptionRegistry | undefined, optionName: string): CheckpointOptions<unknown[]> | undefined {
  return ownDataProperty(registry, optionName, "native options") as CheckpointOptions<unknown[]> | undefined;
}

function ownDataProperty(value: unknown, key: string, label: string): unknown {
  if (value === undefined) return undefined;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} must be a plain object.`);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) throw new TypeError(`${label} must be a plain object.`);
  const descriptor = Object.getOwnPropertyDescriptor(value, key);
  if (!descriptor) return undefined;
  if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError(`${label} entries must be enumerable data properties.`);
  return descriptor.value;
}

function ownDataEntries(value: unknown, label: string): Array<[string, unknown]> {
  if (value === undefined) return [];
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} must be a plain object.`);
  const prototype = Object.getPrototypeOf(value);
  if (prototype !== Object.prototype && prototype !== null) throw new TypeError(`${label} must be a plain object.`);
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.some(key => typeof key !== "string")) throw new TypeError(`${label} cannot contain symbol keys.`);
  return (keys as string[]).map(key => {
    const descriptor = descriptors[key]!;
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError(`${label} entries must be enumerable data properties.`);
    return [key, descriptor.value];
  });
}

function snapshotConnection(value: unknown): AgentConnection {
  if (value === undefined) return {};
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.getPrototypeOf(value) !== Object.prototype) {
    throw new TypeError("Agent connection options must be a plain object.");
  }
  const descriptors = Object.getOwnPropertyDescriptors(value);
  const keys = Reflect.ownKeys(descriptors);
  if (keys.some(key => typeof key !== "string" || !connectionOptionKeys.has(key as string))) throw new TypeError("Unknown Norma agent connection option.");
  const result: Record<string, unknown> = {};
  for (const key of keys as string[]) {
    const descriptor = descriptors[key]!;
    if (!descriptor.enumerable || !Object.hasOwn(descriptor, "value")) throw new TypeError("Agent connection options cannot contain accessors.");
    result[key] = descriptor.value;
  }
  if (result.controlContract !== undefined) {
    result.controlContract = normalizeControlContract(result.controlContract as AgentConnection["controlContract"]);
  }
  return result as AgentConnection;
}

function descriptorsMatch(actual: PropertyDescriptor | undefined, expected: PropertyDescriptor | undefined): boolean {
  if (!actual || !expected) return actual === expected;
  return actual.value === expected.value && actual.writable === expected.writable
    && actual.enumerable === expected.enumerable && actual.configurable === expected.configurable;
}

function jsonSnapshot(value: unknown): JsonObject {
  return copyCheckpointState(value);
}

function describeFile(path: string): { path: string; agentName: string; format: "json" | "yaml" } {
  const extension = extname(path).toLowerCase();
  const format = extension === ".json" ? "json" : extension === ".yaml" || extension === ".yml" ? "yaml" : undefined;
  if (!format) throw new TypeError("Instrumentation files must use .json, .yaml or .yml.");
  const agentName = parsePath(path).name;
  validateAgentName(agentName);
  return { path, agentName, format };
}
async function loadSource(source: string | InstrumentationConfig, options: ConfigureOptions): Promise<Array<{ agentName: string; config: InstrumentationConfig }>> {
  if (typeof source !== "string") {
    if (!options.agentName) throw new TypeError("In-memory instrumentation requires an explicit agentName.");
    validateAgentName(options.agentName);
    return [{ agentName: options.agentName, config: validateConfig(source) }];
  }
  if (options.agentName !== undefined) throw new TypeError("agentName is supplied by the file name or directory entry.");
  const sourceStat = await stat(source);
  let files: string[];
  if (sourceStat.isDirectory()) {
    files = (await readdir(source, { withFileTypes: true }))
      .filter(entry => entry.isFile() && [".json", ".yaml", ".yml"].includes(extname(entry.name).toLowerCase()))
      .map(entry => join(source, entry.name)).sort();
    if (!files.length) throw new TypeError("Instrumentation directory must contain at least one .json, .yaml or .yml file.");
  } else if (sourceStat.isFile()) files = [source];
  else throw new TypeError("Instrumentation source must be a regular file or directory.");
  const seen = new Set<string>();
  const configs: Array<{ agentName: string; config: InstrumentationConfig }> = [];
  for (const path of files) {
    const file = describeFile(path);
    if (seen.has(file.agentName)) throw new TypeError(`Multiple instrumentation files resolve to agent '${file.agentName}'.`);
    seen.add(file.agentName);
    configs.push({ agentName: file.agentName, config: parseInstrumentation(await readFile(path, "utf8"), file.format) });
  }
  return configs;
}

function configuredSchemaSteps(config: InstrumentationConfig): StepSchema[] {
  if (config.version !== 2) return [];
  return (config.schemas?.steps ?? []).map(step => ({
    type: step.type,
    name: step.name,
    ...(step.description === undefined ? {} : { description: step.description }),
    ...(step.input_schema === undefined ? {} : { inputSchema: step.input_schema }),
    ...(step.output_schema === undefined ? {} : { outputSchema: step.output_schema }),
    ...(step.metadata === undefined ? {} : { metadata: step.metadata }),
  }));
}
function knownAgentSchemas(serverSteps: StepSchema[] | undefined, config: InstrumentationConfig): StepSchema[] {
  const server = new Map((serverSteps ?? []).map(step => [`${step.type}\0${step.name}`, step]));
  const configured = new Map<string, StepSchema>();
  for (const [name, item] of Object.entries(config.checkpoints)) {
    const type = item.step_type ?? "tool";
    const key = `${type}\0${name}`;
    configured.set(key, server.get(key) ?? { name, type });
  }
  for (const step of configuredSchemaSteps(config)) configured.set(`${step.type}\0${step.name}`, step);
  return [...configured.values()];
}
function sameJson(left: unknown, right: unknown): boolean {
  if (left === right) return true;
  if (left === null || right === null || typeof left !== "object" || typeof right !== "object") return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const compareKeys = (x: string, y: string): number => x < y ? -1 : x > y ? 1 : 0;
  const a = Object.entries(left).sort(([x], [y]) => compareKeys(x, y));
  const b = Object.entries(right).sort(([x], [y]) => compareKeys(x, y));
  return a.length === b.length && a.every(([key, value], index) => key === b[index]![0] && sameJson(value, b[index]![1]));
}

export interface ConfiguredAgents {
  readonly clients: Readonly<Record<string, NormaClient>>;
  readonly schemas: Readonly<Record<string, readonly StepSchema[]>>;
  withAgent<T>(agentName: string, callback: () => T): T;
  uninstall(): void;
  close(): Promise<Readonly<Record<string, DeliveryReceipt>>>;
}

/** Load, remotely validate, then transactionally install one or more agent scopes. */
export async function configure(source: string | InstrumentationConfig, options: ConfigureOptions): Promise<ConfiguredAgents> {
  const loaded = await loadSource(source, options);
  const agentNames = new Set(loaded.map(item => item.agentName));
  if (agentNames.size !== loaded.length) throw new TypeError("Each configured agent name must be unique.");
  for (const { agentName } of loaded) validateAgentName(agentName);
  if (!options.bindings || typeof options.bindings !== "object" || Array.isArray(options.bindings)
      || (Object.getPrototypeOf(options.bindings) !== Object.prototype && Object.getPrototypeOf(options.bindings) !== null)) {
    throw new TypeError("bindings must be an explicit root object.");
  }
  const sharedConnection = snapshotConnection(options.connection);
  const connectionOverrides = ownDataEntries(options.connections, "connections");
  for (const [key] of connectionOverrides) if (!agentNames.has(key)) throw new TypeError(`Connection override references unknown agent '${key}'.`);
  const fileConnections = new Map<string, AgentConnection>();
  for (const { agentName, config } of loaded) {
    validateAllParamsParameterNames(config);
    validateTypeScriptV2Capabilities(config);
    const override = ownDataProperty(options.connections, agentName, "connections");
    const highestConnection = override instanceof NormaClient ? connectionFor(override) ?? {} : snapshotConnection(override);
    try {
      fileConnections.set(agentName, resolveTypeScriptV2Connection(config, options.bindings, { ...sharedConnection, ...highestConnection }) as AgentConnection);
    } catch (error) {
      const agentKey = config.version === 2 ? config.connection?.agent_key : undefined;
      if (agentKey && "binding" in agentKey) {
        throw new TypeError(`Connection source '/connection/agent_key' requires ConfigureOptions.bindings reference '${agentKey.binding}' to resolve to a valid key.`);
      }
      throw error;
    }
  }
  const clients: Record<string, NormaClient> = Object.create(null) as Record<string, NormaClient>;
  for (const { agentName } of loaded) {
    const override = ownDataProperty(options.connections, agentName, "connections");
    if (override instanceof NormaClient) {
      if (!override.initialized || override.config?.agentName !== agentName) throw new TypeError(`Supplied client does not match configured agent '${agentName}'.`);
      const expected = { ...fileConnections.get(agentName), ...sharedConnection };
      const actual = connectionFor(override);
      for (const [key, value] of Object.entries(expected)) {
        if (actual?.[key as keyof typeof actual] !== value) {
          throw new TypeError(`Supplied NormaClient conflicts with serialized connection setting '${key}' for agent '${agentName}'.`);
        }
      }
      clients[agentName] = override;
    } else {
      const client = new NormaClient();
      client.init({ ...fileConnections.get(agentName), ...sharedConnection, ...snapshotConnection(override), agentName });
      clients[agentName] = client;
    }
  }
  const clientRevisions = Object.fromEntries(Object.entries(clients).map(([name, client]) => [name, client.connectionRevision]));
  const installationToken = Symbol("Norma configured-agent installation");
  let installationActive = true;
  const assertConfiguredSessionActive = (agentName: string): void => {
    const client = clients[agentName];
    if (!installationActive || !client?.initialized || client.connectionRevision !== clientRevisions[agentName]) {
      throw new CheckError({ stage: "pre", message: "Configured Norma identity is no longer active; execution was blocked." });
    }
  };

  const checkpointPlans: CheckpointPlan[] = [];
  const operationPlans: OperationPlan[] = [];
  const entrypointPlans: EntrypointPlan[] = [];
  const adapterPlans: PreparedAdapterInstall[] = [];
  const checkpointBySlot = new Map<string, Set<string>>();
  const entrypointBySlot = new Map<string, string>();
  const isVersion2 = (name: string): boolean => loaded.find(item => item.agentName === name)?.config.version === 2;
  for (const { agentName, config } of loaded) {
    for (const configuredEntrypoint of config.entrypoints) {
      const entrypoint = typeof configuredEntrypoint === "string" ? undefined : configuredEntrypoint;
      const parameterNames = entrypoint?.parameter_names;
      const path = entrypoint?.target ?? configuredEntrypoint as string;
      const configuredFinish = entrypoint?.turn?.finish;
      const streamFinish = configuredFinish && "stream" in configuredFinish ? configuredFinish : undefined;
      if (entrypoint?.returns === "async_iterable" && !streamFinish) {
        throw new TypeError(`Entrypoint '${path}' may declare returns: "async_iterable" only with turn.finish.stream.`);
      }
      const slot = resolveTarget(options.bindings, path, config.version === 2 && streamFinish !== undefined);
      const id = slotId(slot);
      let streamReturn: EntrypointPlan["streamReturn"];
      if (streamFinish) {
        const asyncGenerator = isNativeAsyncGeneratorFunction(slot.value);
        const asyncFunction = isNativeAsyncFunction(slot.value);
        if (asyncGenerator && entrypoint?.returns === "promise") {
          throw new TypeError(`Async generator entrypoint '${path}' returns an AsyncIterable directly; remove returns: "promise".`);
        }
        if (asyncFunction && entrypoint?.returns === "async_iterable") {
          throw new TypeError(`Async entrypoint '${path}' returns a Promise; use returns: "promise" or omit the declaration.`);
        }
        if (!asyncGenerator && !asyncFunction && entrypoint?.returns !== "async_iterable" && entrypoint?.returns !== "promise") {
          throw new TypeError(`Stream entrypoint '${path}' must be an async generator or declare returns: "async_iterable" or "promise".`);
        }
        if (entrypoint?.returns === "promise") requirePromiseDeclaration(slot.value, "promise", `Turn entrypoint '${path}'`);
        streamReturn = asyncGenerator || entrypoint?.returns === "async_iterable" ? "async_iterable" : "promise";
      } else if (entrypoint?.turn) {
        if (entrypoint.returns === "async_iterable") throw new TypeError(`Entrypoint '${path}' may declare returns: "async_iterable" only with turn.finish.stream.`);
        requirePromiseDeclaration(slot.value, entrypoint.returns, `Turn entrypoint '${path}'`);
      }
      const previousAgent = entrypointBySlot.get(id);
      if (previousAgent) throw new TypeError(`Entrypoint '${path}' is already assigned to agent '${previousAgent}'.`);
      if (checkpointBySlot.has(id) && (!isVersion2(agentName) || [...checkpointBySlot.get(id)!].some(owner => !isVersion2(owner)))) {
        throw new TypeError(`Entrypoint '${path}' can compose with checkpoint targets only when every participant uses configuration version 2.`);
      }
      entrypointBySlot.set(id, agentName);
      let context: ValueResolver | undefined;
      let targetType: ValueResolver | undefined;
      let targetId: ValueResolver | undefined;
      let traceId: ValueResolver | undefined;
      let spanId: ValueResolver | undefined;
      let turn: PreparedTurn | undefined;
      if (entrypoint?.context) {
        if ("path" in entrypoint.context && ["context", "result", "error"].includes(entrypoint.context.path[0])) {
          throw new TypeError("Entrypoint context selectors cannot read context, result or error roots.");
        }
        context = valueResolver(entrypoint.context, options.bindings, "pre", parameterNames);
      }
      if (entrypoint?.target_scope) {
        targetType = valueResolver(entrypoint.target_scope.type, options.bindings, "pre", parameterNames);
        targetId = valueResolver(entrypoint.target_scope.id, options.bindings, "pre", parameterNames);
      }
      if (entrypoint?.trace) {
        traceId = valueResolver(entrypoint.trace.trace_id, options.bindings, "pre", parameterNames);
        spanId = valueResolver(entrypoint.trace.span_id, options.bindings, "pre", parameterNames);
      }
      if (entrypoint?.turn) {
        const config = entrypoint.turn;
        if (config.finish && config.finish.driver !== undefined && config.finish.driver !== "generic") {
          throw new TypeError(`Turn finish driver '${config.finish.driver}' is not supported by the TypeScript SDK; use its explicit generic finish configuration.`);
        }
        const callback = (reference: BindingReference | undefined, label: string) => {
          if (!reference) return undefined;
          const resolved = resolveBinding(options.bindings, reference.binding);
          if (typeof resolved.value !== "function") throw new TypeError(`${label} binding '${reference.binding}' must resolve to a callback.`);
          return { owner: resolved.owner, callback: resolved.value as (...args: unknown[]) => unknown };
        };
        turn = {
          config,
          sessionId: valueResolver(config.session_id, options.bindings, "pre", parameterNames),
          turnId: valueResolver(config.turn_id, options.bindings, "pre", parameterNames),
          ...(config.user_request ? { userRequest: valueResolver(config.user_request, options.bindings, "pre", parameterNames) } : {}),
          ...(config.finish && "final_message" in config.finish ? { finalMessage: valueResolver(config.finish.final_message, options.bindings, "post", parameterNames) } : {}),
          ...(config.finish && "stream" in config.finish ? { stream: {
            mode: config.finish.stream.mode ?? "concat",
            maxChars: config.finish.stream.max_chars ?? 8000,
            missing: config.finish.stream.missing ?? "error",
            selectText: streamTextResolver(config.finish.stream.text, options.bindings, parameterNames),
          } } : {}),
          ...(config.finish && "continue_with" in config.finish && config.finish.continue_with
            ? { continueWith: callback(config.finish.continue_with, "turn.finish.continue_with") } : {}),
          ...(config.finish?.on_verdict ? { onVerdict: callback(config.finish.on_verdict, "turn.finish.on_verdict") } : {}),
        };
      }
      entrypointPlans.push({ ...slot, agentName, context, targetType, targetId, traceId, spanId, turn, streamReturn });
    }
    const seenInAgent = new Set<string>();
    const stepNames = new Set<string>();
    for (const [stepName, item] of Object.entries(config.checkpoints)) {
      const slot = resolveTarget(options.bindings, item.target);
      const id = slotId(slot);
      const entrypointOwner = entrypointBySlot.get(id);
      if (entrypointOwner && (!isVersion2(agentName) || !isVersion2(entrypointOwner))) {
        throw new TypeError(`Entrypoint '${item.target}' can compose with checkpoint targets only when every participant uses configuration version 2.`);
      }
      if (seenInAgent.has(id)) throw new TypeError(`Checkpoint target '${item.target}' is duplicated for agent '${agentName}'.`);
      seenInAgent.add(id);
      const agents = checkpointBySlot.get(id) ?? new Set<string>();
      agents.add(agentName); checkpointBySlot.set(id, agents);
      const nativeSource = typeof item.options === "string" ? optionsFor(options.options, item.options)
        : item.options ? resolveInlineNativeOptions(item.options, options.bindings, agentName, item.parameter_names, stepName) : undefined;
      if (item.options && !nativeSource) throw new TypeError(`Checkpoint '${stepName}' references missing native options '${item.options}'.`);
      const native = nativeSource ? snapshotNativeOptions(nativeSource) : undefined;
      if (native?.stepName !== undefined && native.stepName !== stepName) throw new TypeError(`Native options stepName conflicts with configured checkpoint '${stepName}'.`);
      const stepType = item.step_type ?? "tool";
      if (native?.stepType !== undefined && native.stepType !== stepType) throw new TypeError(`Native options stepType conflicts with configured checkpoint '${stepName}'.`);
      if (stepNames.has(stepName)) throw new TypeError(`Duplicate configured checkpoint name '${stepName}'.`);
      stepNames.add(stepName);
      if (item.state !== undefined && item.state_provider !== undefined) throw new TypeError(`Checkpoint '${stepName}' cannot set state and state_provider together.`);
      if ((item.state !== undefined || item.state_provider !== undefined) && native?.state !== undefined) throw new TypeError(`Checkpoint '${stepName}' has conflicting state sources.`);
      if (item.state_from !== undefined && (item.state !== undefined || item.state_provider !== undefined || native?.state !== undefined)) {
        throw new TypeError(`Checkpoint '${stepName}' has conflicting state sources.`);
      }
      if (item.action !== undefined && native?.action !== undefined) throw new TypeError(`Checkpoint '${stepName}' has conflicting action sources.`);
      const stateFrom = item.state_from ? checkpointSourceResolver(item.state_from, options.bindings, "pre", item.parameter_names, stepName, "state_from") : undefined;
      const inputFrom = item.input_from === "all_params"
        ? (invocation: InvocationValues) => cloneJson(selectorRootValue("params", invocation, item.parameter_names))
        : item.input_from && item.input_from !== "all_kwargs" ? checkpointSourceResolver(item.input_from, options.bindings, "pre", item.parameter_names, stepName, "input_from") : undefined;
      const contextFrom = item.context_from ? checkpointSourceResolver(item.context_from, options.bindings, "pre", item.parameter_names, stepName, "context_from") : undefined;
      const toolsFrom = item.tools_from ? checkpointSourceResolver(item.tools_from, options.bindings, "pre", item.parameter_names, stepName, "tools_from") : undefined;
      const groundTruthFrom = item.ground_truth_from ? checkpointSourceResolver(item.ground_truth_from, options.bindings, "pre", item.parameter_names, stepName, "ground_truth_from") : undefined;
      const outputFrom = item.output_from ? checkpointSourceResolver(item.output_from, options.bindings, "post", item.parameter_names, stepName, "output_from") : undefined;
      let stateProvider: unknown;
      if (item.state_provider !== undefined) {
        try { stateProvider = ownDataProperty(options.providers, item.state_provider, "providers"); }
        catch { throw new TypeError(`Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/state_provider' requires ConfigureOptions.providers to contain the exact key '${item.state_provider}'.`); }
      }
      if (item.state_provider !== undefined && typeof stateProvider !== "function") {
        throw new TypeError(`Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/state_provider' requires ConfigureOptions.providers to contain the exact key '${item.state_provider}' resolving to a callback.`);
      }
      const configuredClient = clients[agentName]!;
      if (native?.client && native.client !== configuredClient) throw new TypeError(`Native options client must be the configured client for agent '${agentName}'.`);
      const targetType = native?.targetType ?? configuredClient.config?.targetType;
      const targetId = native?.targetId ?? configuredClient.config?.targetId;
      if (Boolean(targetType) !== Boolean(targetId)) throw new TypeError(`Checkpoint '${stepName}' requires targetType and targetId together.`);
      const merged: CheckpointOptions<unknown[]> = {
        ...(native ?? {}), client: configuredClient, stepName, stepType,
        targetScope: () => {
          const scope = agentContext.getStore();
          return scope?.token === installationToken && scope.agentName === agentName
            ? { targetType: scope.targetType, targetId: scope.targetId } : undefined;
        },
        traceContext: () => {
          const scope = agentContext.getStore();
          return scope?.token === installationToken && scope.agentName === agentName
            ? { traceId: scope.traceId, spanId: scope.spanId } : undefined;
        },
        ...(item.state !== undefined ? { state: jsonSnapshot(item.state) } : {}),
        ...(item.state_provider !== undefined ? { state: async (...args: unknown[]) => {
          try { return copyCheckpointState(await (stateProvider as StateProvider)(...args)); }
          catch { throw configuredSourceCheckError("pre", `Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/state_provider' failed during pre; verify the configured provider and its JSON value.`); }
        } } : {}),
        ...(stateFrom ? { state: async (...args: unknown[]) => {
          try {
            const captured = await stateFrom({ args, kwargs: {}, context: contextFor(agentName), agent_name: agentName });
            if (!captured || typeof captured !== "object" || Array.isArray(captured)) throw new TypeError();
            return captured as JsonObject;
          } catch {
            throw configuredSourceCheckError("pre", `Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/state_from' failed during pre; verify the configured source and its JSON value.`);
          }
        } } : {}),
        ...(item.action ? { action: actionResolver(item.action, options.bindings, agentName, item.parameter_names,
          { label: `Checkpoint '${stepName}'`, pointer: `/checkpoints/${pointerSegment(stepName)}/action` }) } : {}),
        ...((inputFrom || contextFrom || toolsFrom || groundTruthFrom || outputFrom || item.input_schema) ? { payload: {
          pre: async (args: unknown[]) => {
            const invocation: InvocationValues = { args, kwargs: {}, context: contextFor(agentName), agent_name: agentName };
            const [input, context, tools, groundTruth] = await Promise.all([
              inputFrom?.(invocation), contextFrom?.(invocation), toolsFrom?.(invocation), groundTruthFrom?.(invocation),
            ]);
            if (item.input_schema !== undefined) {
              const finalInput = input !== undefined ? input : item.step_type === "llm" ? args : { args };
              validateCapturedInput(item, finalInput, stepName);
            }
            if (context !== undefined && (!context || typeof context !== "object" || Array.isArray(context))) {
              throw configuredSourceCheckError("pre", `Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/context_from' must resolve to a JSON object.`);
            }
            if (context && ["norma_action", "norma_argument_shape", "state"].some(key => Object.hasOwn(context, key))) {
              throw configuredSourceCheckError("pre", `Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/context_from' cannot replace Norma-owned context fields.`);
            }
            if (tools !== undefined && (!Array.isArray(tools) || tools.some(tool => !tool || typeof tool !== "object" || Array.isArray(tool)))) {
              throw configuredSourceCheckError("pre", `Checkpoint '${stepName}' source '/checkpoints/${pointerSegment(stepName)}/tools_from' must resolve to a JSON array of objects.`);
            }
            return {
              ...(input === undefined ? {} : { input }),
              ...(context === undefined ? {} : { context: context as JsonObject }),
              ...(tools === undefined ? {} : { tools: tools as JsonObject[] }),
              ...(groundTruth === undefined ? {} : { groundTruth }),
            };
          },
          post: async (args: unknown[], result: unknown) => {
            if (!outputFrom) return { output: cloneJson(result) };
            return { output: await outputFrom({ args, kwargs: {}, result, context: contextFor(agentName), agent_name: agentName }) };
          },
        } } : {}),
      };
      checkpointPlans.push({ ...slot, agentName, stepName, stepType, options: merged });
    }
  }
  const occupiedSlots = new Set([...checkpointPlans, ...entrypointPlans].map(slotId));
  const operationSlotsByAgent = new Set<string>();
  const compileObservation = (
    observation: InstrumentationOperationObservation,
    phase: "pre" | "post",
    parameterNames?: readonly string[],
  ): CompiledObservation => {
    return {
      eventKey: valueResolver(observation.event_key, options.bindings, phase, parameterNames),
      summary: valueResolver(observation.summary, options.bindings, phase, parameterNames),
      ...(observation.external_reference ? { externalReference: valueResolver(observation.external_reference, options.bindings, phase, parameterNames) } : {}),
    };
  };
  for (const { agentName, config } of loaded) {
    if (config.version !== 2 || !config.operations) continue;
    for (const [operationName, item] of Object.entries(config.operations)) {
      const slot = resolveTarget(options.bindings, item.target);
      const id = slotId(slot);
      if (occupiedSlots.has(id)) throw new TypeError(`Operation target '${item.target}' cannot share a slot with an entrypoint or checkpoint.`);
      const agentSlot = `${agentName}\0${id}`;
      if (operationSlotsByAgent.has(agentSlot)) throw new TypeError(`Operation target '${item.target}' is duplicated for agent '${agentName}'.`);
      operationSlotsByAgent.add(agentSlot);
      requirePromiseDeclaration(slot.value, item.returns, `Operation target '${item.target}'`);
      const bindCallback = (reference: BindingReference | undefined, label: string) => {
        if (!reference) return undefined;
        const callback = resolveBinding(options.bindings, reference.binding);
        if (typeof callback.value !== "function") throw new TypeError(`${label} must resolve to a callback.`);
        return { owner: callback.owner, callback: callback.value as (...args: unknown[]) => unknown };
      };
      const observations = item.observations;
      const operationPlan: CompiledOperationPlan = {
        config: item,
        operationName,
        operationKey: valueResolver(item.operation_key, options.bindings, "pre", item.parameter_names),
        ...(item.input ? { input: valueResolver(item.input, options.bindings, "pre", item.parameter_names) } : {}),
        ...(item.context ? { context: valueResolver(item.context, options.bindings, "pre", item.parameter_names) } : {}),
        ...(item.evidence ? { evidence: valueResolver(item.evidence, options.bindings, "pre", item.parameter_names) } : {}),
        ...(item.tools ? { tools: valueResolver(item.tools, options.bindings, "pre", item.parameter_names) } : {}),
        ...(observations?.attempted ? { attempted: compileObservation(observations.attempted, "pre", item.parameter_names) } : {}),
        ...(observations?.succeeded ? { succeeded: compileObservation(observations.succeeded, "post", item.parameter_names) } : {}),
        ...(observations?.failed ? { failed: compileObservation(observations.failed, "post", item.parameter_names) } : {}),
        ...(item.verification_session_id ? { verificationSessionId: valueResolver(item.verification_session_id, options.bindings, "post", item.parameter_names) } : {}),
        ...(item.action ? { action: actionResolver(item.action, options.bindings, agentName, item.parameter_names,
          { label: `Operation '${operationName}'`, pointer: `/operations/${pointerSegment(operationName)}/action` }) as (...args: unknown[]) => import("./checkpoint").NormaAction | Promise<import("./checkpoint").NormaAction> } : {}),
        ...(item.on_record ? { onRecord: bindCallback(item.on_record, `operations.${operationName}.on_record`) } : {}),
      };
      operationPlans.push({ ...slot, agentName, plan: operationPlan });
    }
  }
  for (const [name, value] of ownDataEntries(options.providers, "providers")) if (!name.trim() || typeof value !== "function") throw new TypeError("State providers must be named callbacks.");
  for (const [key, value] of ownDataEntries(options.options, "native options")) {
    if (!key.trim()) throw new TypeError("Native option names must be nonblank.");
    snapshotNativeOptions(value);
  }

  const adapterOwners = new Map<object, string>();
  const adapterToolsOwners = new Map<unknown[], string>();
  for (const { agentName, config } of loaded) {
    if (config.version !== 2 || !config.adapters) continue;
    for (const [adapterName, adapter] of Object.entries(config.adapters)) {
      if (adapter.kind !== "openai_agents") continue;
      const prepared = await prepareOpenAIAgentAdapter(agentName, adapter, options.bindings, clients[agentName]!, () => {
        const scope = agentContext.getStore();
        const active = installationActive && clients[agentName]?.initialized === true
          && clients[agentName]?.connectionRevision === clientRevisions[agentName];
        if (!active) return { context: {}, active: false };
        if (scope?.token === installationToken && scope.agentName === agentName
            && scope.revision === clientRevisions[agentName] && clients[agentName]?.connectionRevision === scope.revision) {
          return { targetType: scope.targetType, targetId: scope.targetId, traceId: scope.traceId, spanId: scope.spanId, context: scope.context, active };
        }
        const identity = clients[agentName]?.config;
        return {
          ...(identity?.targetType && identity.targetId ? { targetType: identity.targetType, targetId: identity.targetId } : {}),
          context: {}, active,
        };
      });
      const previous = adapterOwners.get(prepared.nativeAgent);
      if (previous) throw new TypeError(`Native OpenAI Agent is already assigned to '${previous}'; adapter '${adapterName}' cannot claim it twice.`);
      adapterOwners.set(prepared.nativeAgent, `${agentName}:${adapterName}`);
      const previousToolsOwner = adapterToolsOwners.get(prepared.toolsArray);
      if (previousToolsOwner) throw new TypeError(`Native OpenAI tools array is already owned by '${previousToolsOwner}'; adapter '${adapterName}' cannot share its mutable installation slot.`);
      adapterToolsOwners.set(prepared.toolsArray, `${agentName}:${adapterName}`);
      adapterPlans.push(prepared);
    }
  }

  // Complete every read-only auth/identity/rule check before mutating application slots.
  const validations = await Promise.all(loaded.map(async ({ agentName }) => {
    const client = clients[agentName]!;
    const response = await client.agents.get({ agentName });
    if (response.agent.agentName !== agentName) throw new Error(`Norma returned an unexpected agent identity while validating '${agentName}'.`);
    const identity = client.config;
    const rules = await client.agents.listRules({
      agentName,
      enabledState: "enabled",
      ...(identity?.targetType && identity.targetId
        ? { targetType: identity.targetType, targetId: identity.targetId }
        : {}),
    });
    if (rules.rules.some(rule => rule.status !== "hosted" || rule.definition === null)) {
      throw new Error(`Agent '${agentName}' has a rule definition this SDK cannot validate safely.`);
    }
    return [agentName, response.steps ?? []] as const;
  }));
  if (Object.entries(clients).some(([name, client]) => client.connectionRevision !== clientRevisions[name])) {
    throw new Error("A Norma client was reinitialized during declarative startup validation.");
  }
  const serverSchemas = new Map(validations);
  // Validate every declared expectation before the first optional schema write.
  for (const { agentName, config } of loaded) {
    const server = new Map((serverSchemas.get(agentName) ?? []).map(step => [`${step.type}\0${step.name}`, step]));
    const version2 = config.version === 2 ? config : undefined;
    for (const expected of configuredSchemaSteps(config)) {
      const existing = server.get(`${expected.type}\0${expected.name}`);
      if (!existing && !version2?.schemas?.publish) {
        throw new TypeError(`Expected step schema '${expected.type}:${expected.name}' is not registered for agent '${agentName}'.`);
      }
      if (existing && (!sameJson(existing.inputSchema ?? null, expected.inputSchema ?? null)
          || !sameJson(existing.outputSchema ?? null, expected.outputSchema ?? null))) {
        throw new TypeError(`Expected step schema '${expected.type}:${expected.name}' conflicts with its registered input or output schema.`);
      }
    }
  }
  const schemas: Record<string, readonly StepSchema[]> = {};
  for (const { agentName, config } of loaded) {
    const server = serverSchemas.get(agentName) ?? [];
    const version2 = config.version === 2 ? config : undefined;
    if (version2?.schemas?.publish) {
      const known = new Set(server.map(step => `${step.type}\0${step.name}`));
      const submitted = [...configuredSchemaSteps(config)];
      const submittedKeys = new Set(submitted.map(step => `${step.type}\0${step.name}`));
      for (const [name, checkpointConfig] of Object.entries(config.checkpoints)) {
        const type = checkpointConfig.step_type ?? "tool";
        const key = `${type}\0${name}`;
        if (!known.has(key) && !submittedKeys.has(key)) submitted.push({ type, name });
      }
      const published = submitted.length ? await publishAgentStepsFor(clients[agentName]!, submitted) : server;
      schemas[agentName] = Object.freeze([...published]);
    } else {
      schemas[agentName] = Object.freeze(knownAgentSchemas(server, config));
    }
  }

  const wrappersBySlot = new Map<string, { slot: Slot; wrapper: (...args: never[]) => unknown; handlers?: Map<string, (...args: unknown[]) => Promise<unknown>> }>();
  const handlersBySlot = new Map<string, Map<string, (...args: unknown[]) => Promise<unknown>>>();
  const entrypointById = new Map(entrypointPlans.map(plan => [slotId(plan), plan]));
  const entrypointRunners = new Map<string, (...args: unknown[]) => unknown>();
  for (const plan of entrypointPlans) {
    const id = slotId(plan);
    entrypointRunners.set(id, function (this: unknown, ...args: unknown[]): unknown {
      if (!installationActive || clients[plan.agentName]?.connectionRevision !== clientRevisions[plan.agentName]) throw new CheckError({ stage: "pre", message: "Configured Norma identity is no longer active; execution was blocked." });
      if (plan.turn?.stream && plan.streamReturn) {
        const turn = plan.turn;
        const streamFinish = turn.stream!;
        const stream = configuredAsyncStream({
          mode: streamFinish.mode,
          maxChars: streamFinish.maxChars,
          missing: streamFinish.missing,
          open: async (): Promise<ConfiguredStreamSession> => {
            assertConfiguredSessionActive(plan.agentName);
            const initial: InvocationValues = { args, kwargs: {}, context: {}, agent_name: plan.agentName };
            const contextValue = plan.context ? await plan.context(initial) : {};
            assertConfiguredSessionActive(plan.agentName);
            if (!contextValue || typeof contextValue !== "object" || Array.isArray(contextValue)) {
              throw new TypeError("Entrypoint context must resolve to a JSON object.");
            }
            const scopedContext = { ...contextValue as JsonObject };
            const invocation: InvocationValues = { args, kwargs: {}, context: scopedContext, agent_name: plan.agentName };
            const selections = [plan.targetType?.(invocation), plan.targetId?.(invocation), plan.traceId?.(invocation), plan.spanId?.(invocation)];
            const [targetType, targetId, traceIdValue, spanIdValue] = await Promise.all(selections);
            assertConfiguredSessionActive(plan.agentName);
            if ((targetType !== undefined && typeof targetType !== "string") || (targetId !== undefined && typeof targetId !== "string")) {
              throw new TypeError("Entrypoint target_scope selectors must resolve to strings.");
            }
            const scope: AgentScope = { token: installationToken, agentName: plan.agentName,
              revision: clientRevisions[plan.agentName]!, context: scopedContext,
              targetType: targetType as string | undefined, targetId: targetId as string | undefined };
            if (Boolean(scope.targetType) !== Boolean(scope.targetId)
                || (scope.targetType !== undefined && (!scope.targetType.trim() || !scope.targetId?.trim()))) {
              throw new TypeError("Entrypoint target_scope type and id must both resolve to nonblank strings.");
            }
            if ((traceIdValue !== undefined && typeof traceIdValue !== "string") || (spanIdValue !== undefined && typeof spanIdValue !== "string")) {
              throw new TypeError("Entrypoint trace selectors must resolve to strings.");
            }
            const traceId = traceIdValue as string | undefined;
            const spanId = spanIdValue as string | undefined;
            if (Boolean(traceId) !== Boolean(spanId)
                || (traceId !== undefined && (!/^(?!0{32})[0-9a-fA-F]{32}$/.test(traceId) || !spanId || !/^(?!0{16})[0-9a-fA-F]{16}$/.test(spanId)))) {
              throw new TypeError("Entrypoint trace IDs must be paired, nonzero W3C hexadecimal identifiers.");
            }
            scope.traceId = traceId;
            scope.spanId = spanId;

            const turnInvocation: InvocationValues = { args, kwargs: {}, context: scopedContext, agent_name: plan.agentName };
            const [sessionId, turnId, userRequest] = await Promise.all([
              turn.sessionId(turnInvocation), turn.turnId(turnInvocation), turn.userRequest?.(turnInvocation),
            ]);
            assertConfiguredSessionActive(plan.agentName);
            if (typeof sessionId !== "string" || !sessionId.trim() || typeof turnId !== "string" || !turnId.trim()) {
              throw new TypeError("turn.session_id and turn.turn_id must resolve to nonblank strings.");
            }
            if (userRequest !== undefined && typeof userRequest !== "string") throw new TypeError("turn.user_request must resolve to a string.");
            const recorder = new TurnRecorder({ sessionId, turnId, ...(userRequest === undefined ? {} : { userRequest }),
              ...(turn.config.platform === undefined ? {} : { platform: turn.config.platform }),
              ...(turn.config.platform_version === undefined ? {} : { platformVersion: turn.config.platform_version }),
              ...(turn.config.model === undefined ? {} : { model: turn.config.model }),
              maxContinuations: turn.config.max_continuations ?? 2 });
            let iterator: AsyncIterator<unknown> | undefined;
            const startSource = async (): Promise<void> => {
              if (iterator) return;
              assertConfiguredSessionActive(plan.agentName);
              assertConfiguredSlotOwnership();
              const target = handlersBySlot.get(id)?.get(plan.agentName) ?? plan.value;
              const pendingResult = Reflect.apply(target, this, args);
              let iterable: unknown = pendingResult;
              if (plan.streamReturn === "promise") {
                requirePromiseResult(pendingResult, `Stream entrypoint '${plan.path}'`);
                iterable = await pendingResult;
              }
              iterator = asyncIteratorFrom(iterable);
              assertConfiguredSessionActive(plan.agentName);
            };
            const streamSession: ConfiguredStreamSession = {
              run: work => agentContext.run(scope, () => recorder.run(work)),
              assertActive: () => assertConfiguredSessionActive(plan.agentName),
              get iterator() { return iterator; },
              start: startSource,
              selectText: chunk => streamFinish.selectText({ ...turnInvocation, result: chunk }),
              async finalize(finalMessage) {
                const finish = turn.config.finish;
                if (!finish || !("stream" in finish)) return;
                const record = recorder.turn(finalMessage, 0);
                const verdict = await checkFinish(record, {
                  client: clients[plan.agentName], agentName: plan.agentName,
                  ...(scope.targetType === undefined ? {} : { targetType: scope.targetType, targetId: scope.targetId }),
                });
                assertConfiguredSessionActive(plan.agentName);
                if (turn.onVerdict) {
                  await turn.onVerdict.callback.call(turn.onVerdict.owner, { ...turnInvocation, result: finalMessage, turn: record, verdict });
                }
                assertConfiguredSessionActive(plan.agentName);
                if (verdict.verdict === "hold" && finish.on_hold !== "return") throw new FinishHeldError(finalMessage, verdict);
              },
            };
            return streamSession;
          },
        });
        return plan.streamReturn === "promise" ? Promise.resolve(stream) : stream;
      }
      const initial: InvocationValues = { args, kwargs: {}, context: {}, agent_name: plan.agentName };
      const contextValue = plan.context ? plan.context(initial) : {};
      const activateContext = (context: JsonValue): unknown => {
        if (!context || typeof context !== "object" || Array.isArray(context)) throw new TypeError("Entrypoint context must resolve to a JSON object.");
        const scopedContext = { ...context as JsonObject };
        const invocation: InvocationValues = { args, kwargs: {}, context: scopedContext, agent_name: plan.agentName };
        const selections = [plan.targetType?.(invocation), plan.targetId?.(invocation), plan.traceId?.(invocation), plan.spanId?.(invocation)];
        const activate = ([targetType, targetId, traceIdValue, spanIdValue]: unknown[]): unknown => {
          if ((targetType !== undefined && typeof targetType !== "string") || (targetId !== undefined && typeof targetId !== "string")) throw new TypeError("Entrypoint target_scope selectors must resolve to strings.");
          const scope: AgentScope = { token: installationToken, agentName: plan.agentName, revision: clientRevisions[plan.agentName]!, context: scopedContext,
            targetType: targetType as string | undefined,
            targetId: targetId as string | undefined };
          if (Boolean(scope.targetType) !== Boolean(scope.targetId) || (scope.targetType !== undefined && (!scope.targetType.trim() || !scope.targetId?.trim()))) throw new TypeError("Entrypoint target_scope type and id must both resolve to nonblank strings.");
          if ((traceIdValue !== undefined && typeof traceIdValue !== "string") || (spanIdValue !== undefined && typeof spanIdValue !== "string")) throw new TypeError("Entrypoint trace selectors must resolve to strings.");
          const traceId = traceIdValue as string | undefined;
          const spanId = spanIdValue as string | undefined;
          if (Boolean(traceId) !== Boolean(spanId) || (traceId !== undefined && (!/^(?!0{32})[0-9a-fA-F]{32}$/.test(traceId) || !spanId || !/^(?!0{16})[0-9a-fA-F]{16}$/.test(spanId)))) throw new TypeError("Entrypoint trace IDs must be paired, nonzero W3C hexadecimal identifiers.");
          scope.traceId = traceId; scope.spanId = spanId;
          const invoke = () => {
            assertConfiguredSessionActive(plan.agentName);
            assertConfiguredSlotOwnership();
            return agentContext.run(scope, () => Reflect.apply(handlersBySlot.get(id)?.get(plan.agentName) ?? plan.value, this, args));
          };
          if (!plan.turn) return invoke();
          const turn = plan.turn;
          const turnInvocation: InvocationValues = { args, kwargs: {}, context: scopedContext, agent_name: plan.agentName };
          const selectors = [turn.sessionId(turnInvocation), turn.turnId(turnInvocation), turn.userRequest?.(turnInvocation)];
          return Promise.all(selectors).then(async ([sessionId, turnId, userRequest]) => {
            assertConfiguredSessionActive(plan.agentName);
            if (typeof sessionId !== "string" || !sessionId.trim() || typeof turnId !== "string" || !turnId.trim()) throw new TypeError("turn.session_id and turn.turn_id must resolve to nonblank strings.");
            if (userRequest !== undefined && typeof userRequest !== "string") throw new TypeError("turn.user_request must resolve to a string.");
            const recorder = new TurnRecorder({ sessionId, turnId, ...(userRequest === undefined ? {} : { userRequest }),
              ...(turn.config.platform === undefined ? {} : { platform: turn.config.platform }),
              ...(turn.config.platform_version === undefined ? {} : { platformVersion: turn.config.platform_version }),
              ...(turn.config.model === undefined ? {} : { model: turn.config.model }), maxContinuations: turn.config.max_continuations ?? 2 });
            return recorder.run(async () => {
              const pendingResult = invoke();
              requirePromiseResult(pendingResult, `Turn entrypoint '${plan.path}'`);
              const originalResult = await pendingResult;
              let result: unknown = originalResult;
              let continuationCount = 0;
              let continuationAgentGeneration: string | null | undefined;
              const finish = turn.config.finish;
              if (!finish || !turn.finalMessage) return result;
              // eslint-disable-next-line no-constant-condition
              while (true) {
                const finalMessage = await turn.finalMessage({ ...turnInvocation, result, context: scopedContext });
                assertConfiguredSessionActive(plan.agentName);
                if (typeof finalMessage !== "string") throw new TypeError("turn.finish.final_message must resolve to a string.");
                const record = recorder.turn(finalMessage, continuationCount, continuationAgentGeneration);
                const verdict = await checkFinish(record, {
                  client: clients[plan.agentName], agentName: plan.agentName,
                  ...(scope.targetType === undefined ? {} : { targetType: scope.targetType, targetId: scope.targetId }),
                });
                assertConfiguredSessionActive(plan.agentName);
                if (turn.onVerdict) await agentContext.run(scope, () => turn.onVerdict!.callback.call(turn.onVerdict!.owner, { ...turnInvocation, result, turn: record, verdict }));
                assertConfiguredSessionActive(plan.agentName);
                if (verdict.verdict !== "hold") return result;
                if (turn.continueWith && continuationCount < recorder.turn(finalMessage).continuation.max) {
                  if (verdict.continuation.agent_generation !== undefined) {
                    continuationAgentGeneration = verdict.continuation.agent_generation;
                  }
                  const next = await agentContext.run(scope, () => turn.continueWith!.callback.call(turn.continueWith!.owner, {
                    ...turnInvocation, result, verdict, turn: record, continuation_count: continuationCount + 1,
                  }));
                  assertConfiguredSessionActive(plan.agentName);
                  continuationCount += 1;
                  result = next;
                  continue;
                }
                if ("on_hold" in finish && finish.on_hold === "return") return result;
                throw new FinishHeldError(result, verdict);
              }
            });
          });
        };
        return selections.some(value => value instanceof Promise) ? Promise.all(selections).then(activate) : activate(selections);
      };
      return contextValue instanceof Promise ? contextValue.then(activateContext) : activateContext(contextValue);
    });
  }
  for (const plan of checkpointPlans) {
    const id = slotId(plan);
    const byAgent = handlersBySlot.get(id) ?? new Map<string, (...args: unknown[]) => Promise<unknown>>();
    const guarded = async function (this: unknown, ...args: unknown[]): Promise<unknown> {
      if (!installationActive || clients[plan.agentName]?.connectionRevision !== clientRevisions[plan.agentName]) {
        throw new CheckError({ stage: "pre", message: "Configured Norma identity is no longer active; execution was blocked." });
      }
      const child = entrypointById.get(id);
      const target = child && child.agentName !== plan.agentName ? entrypointRunners.get(id)! : plan.value;
      return Reflect.apply(target, this, args);
    };
    const wrapped = checkpoint(guarded, plan.options);
    byAgent.set(plan.agentName, wrapped);
    handlersBySlot.set(id, byAgent);
    const dispatcher = markCheckpointWrapper(async function (this: unknown, ...args: unknown[]): Promise<unknown> {
      const scope = agentContext.getStore();
      const target = installationActive && scope?.token === installationToken && scope.revision === clientRevisions[scope.agentName]
        && clients[scope.agentName]?.connectionRevision === scope.revision ? handlersBySlot.get(id)?.get(scope.agentName) : undefined;
      if (target) return Reflect.apply(target, this, args);
      const entrypoint = entrypointRunners.get(id);
      if (installationActive && entrypoint) return Reflect.apply(entrypoint, this, args);
      throw new CheckError({ stage: "pre", message: "No configured agent scope owns this checkpoint target; enter its configured entrypoint or call withAgent()." });
    } as unknown as (...args: never[]) => unknown);
    wrappersBySlot.set(id, { slot: plan, wrapper: dispatcher, handlers: byAgent });
  }
  for (const plan of operationPlans) {
    const id = slotId(plan);
    const byAgent = handlersBySlot.get(id) ?? new Map<string, (...args: unknown[]) => Promise<unknown>>();
    byAgent.set(plan.agentName, wrapConfiguredOperation(
      plan.value as (...args: unknown[]) => unknown,
      plan.plan,
      clients[plan.agentName]!,
      () => contextFor(plan.agentName),
      () => {
        const scope = agentContext.getStore();
        return scope?.token === installationToken && scope.agentName === plan.agentName
          ? { targetType: scope.targetType, targetId: scope.targetId } : undefined;
      },
      () => {
        const scope = agentContext.getStore();
        return installationActive && scope?.token === installationToken && scope.agentName === plan.agentName
          && scope.revision === clientRevisions[plan.agentName] && clients[plan.agentName]?.connectionRevision === scope.revision;
      },
    ));
    handlersBySlot.set(id, byAgent);
    if (!wrappersBySlot.has(id)) {
      const dispatcher = markCheckpointWrapper(async function (this: unknown, ...args: unknown[]): Promise<unknown> {
        const scope = agentContext.getStore();
        const target = installationActive && scope?.token === installationToken && scope.revision === clientRevisions[scope.agentName]
          && clients[scope.agentName]?.connectionRevision === scope.revision ? handlersBySlot.get(id)?.get(scope.agentName) : undefined;
        if (target) return Reflect.apply(target, this, args);
        throw new CheckError({ stage: "pre", message: "No configured agent scope owns this operation target; enter its configured entrypoint or call withAgent()." });
      } as unknown as (...args: never[]) => unknown);
      wrappersBySlot.set(id, { slot: plan, wrapper: dispatcher, handlers: byAgent });
    }
  }
  for (const plan of entrypointPlans) {
    const id = slotId(plan);
    if (!wrappersBySlot.has(id)) wrappersBySlot.set(id, { slot: plan, wrapper: markCheckpointWrapper(entrypointRunners.get(id)! as (...args: never[]) => unknown) });
  }

  const installed: InstalledSlot[] = [];
  const installedAdapters: PreparedAdapterInstall[] = [];
  const rollback = (): void => {
    for (const entry of installed.reverse()) {
      const current = Object.getOwnPropertyDescriptor(entry.owner, entry.key);
      if (current?.value === entry.wrapper) {
        if (entry.original) Object.defineProperty(entry.owner, entry.key, entry.original);
        else Reflect.deleteProperty(entry.owner, entry.key);
      }
    }
  };
  for (const { slot } of wrappersBySlot.values()) {
    const current = Object.getOwnPropertyDescriptor(slot.owner, slot.key);
    if (!descriptorsMatch(current, slot.original)) {
      throw new TypeError(`Instrumentation target '${slot.path}' changed during startup validation.`);
    }
  }
  try {
    for (const { slot, wrapper } of wrappersBySlot.values()) {
      Object.defineProperty(slot.owner, slot.key, { ...slot.descriptor, value: wrapper });
      installed.push({ owner: slot.owner, key: slot.key, original: slot.original, wrapper });
    }
    for (const adapter of adapterPlans) {
      adapter.verifyUnmodified();
    }
    for (const adapter of adapterPlans) {
      adapter.install();
      installedAdapters.push(adapter);
    }
  } catch (error) {
    for (const adapter of installedAdapters.reverse()) adapter.uninstall();
    rollback();
    throw error;
  }

  const assertConfiguredSlotOwnership = (): void => {
    if (!installationActive) throw new CheckError({ stage: "pre", message: "Configured Norma instrumentation is no longer active; execution was blocked." });
    for (const { slot, wrapper } of wrappersBySlot.values()) {
      const current = Object.getOwnPropertyDescriptor(slot.owner, slot.key);
      if (!current || !Object.hasOwn(current, "value") || current.value !== wrapper) {
        throw new CheckError({ stage: "pre", message: `Configured Norma checkpoint target '${slot.path}' changed after installation; execution was blocked.` });
      }
    }
  };

  const frozenClients = Object.freeze({ ...clients });
  const handle: ConfiguredAgents = {
    clients: frozenClients,
    schemas: Object.freeze(Object.fromEntries(Object.entries(schemas).map(([name, steps]) => [name, Object.freeze([...steps])]))),
    withAgent<T>(agentName: string, callback: () => T): T {
      if (!installationActive) throw new Error("Configured agents have been uninstalled.");
      if (!agentNames.has(agentName)) throw new TypeError(`Agent '${agentName}' is not configured here.`);
      if (clients[agentName]?.connectionRevision !== clientRevisions[agentName]) throw new Error("Configured Norma client was reinitialized.");
      assertConfiguredSlotOwnership();
      return agentContext.run({ token: installationToken, agentName, revision: clientRevisions[agentName]!, context: {} }, callback);
    },
    uninstall(): void {
      if (!installationActive) return;
      installationActive = false;
      for (const adapter of installedAdapters.reverse()) adapter.uninstall();
      rollback();
    },
    async close(): Promise<Readonly<Record<string, DeliveryReceipt>>> {
      handle.uninstall();
      const receipts = await Promise.all(Object.entries(clients).map(async ([name, client]) => [name, await client.flushObservability()] as const));
      return Object.freeze(Object.fromEntries(receipts));
    },
  };
  return handle;
}
