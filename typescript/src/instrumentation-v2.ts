// Modified for Norma.
import { HTTPClient } from "./generated/lib/http";
import { normalizeControlContract } from "./control-contract";
import type { InstrumentationConfig, InstrumentationConfigV2 } from "./instrumentation";

export interface ResolvedBinding {
  owner: object;
  value: unknown;
}

const identifierPath = /^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$/;

export class TypeScriptCapabilityError extends TypeError {
  public readonly code = "capability" as const;
  public readonly path: string;
  public constructor(path: string, message = "This configuration feature is not supported by the TypeScript SDK.") {
    super(message);
    this.name = "TypeScriptCapabilityError";
    this.path = path;
  }
}

function unsupported(path: string, message?: string): never { throw new TypeScriptCapabilityError(path, message); }
function escapePointer(value: string): string { return value.replace(/~/g, "~0").replace(/\//g, "~1"); }

function ownRoot(bindings: Record<string, object>, name: string): unknown {
  const descriptor = Object.getOwnPropertyDescriptor(bindings, name);
  if (!descriptor || !Object.hasOwn(descriptor, "value")) {
    throw new TypeError(`Binding root '${name}' must be an own data property.`);
  }
  return descriptor.value;
}

/** Resolve a dotted binding using data descriptors only; methods retain their receiver. */
export function resolveBinding(bindings: Record<string, object>, path: string): ResolvedBinding {
  if (!identifierPath.test(path)) throw new TypeError(`Invalid binding reference '${path}'.`);
  const segments = path.split(".");
  let current = ownRoot(bindings, segments[0]!);
  if ((typeof current !== "object" && typeof current !== "function") || current === null) {
    throw new TypeError(`Binding root '${segments[0]}' must be an object.`);
  }
  let owner = current as object;
  for (const segment of segments.slice(1)) {
    owner = current as object;
    let selected: PropertyDescriptor | undefined;
    let cursor: object | null = current as object;
    while (cursor !== null) {
      selected = Object.getOwnPropertyDescriptor(cursor, segment);
      if (selected) {
        break;
      }
      cursor = Object.getPrototypeOf(cursor) as object | null;
    }
    if (!selected || !Object.hasOwn(selected, "value")) {
      throw new TypeError(`Binding reference '${path}' must resolve through data properties only.`);
    }
    current = selected.value;
  }
  return { owner, value: current };
}

function bindingValue(bindings: Record<string, object>, reference: { binding: string }, label: string): unknown {
  const resolved = resolveBinding(bindings, reference.binding);
  if (resolved.value === undefined) throw new TypeError(`${label} binding '${reference.binding}' is missing.`);
  return typeof resolved.value === "function" ? resolved.value.bind(resolved.owner) : resolved.value;
}

/** Reject settings this SDK cannot honor before it creates clients or sends requests. */
export function validateTypeScriptV2Capabilities(config: InstrumentationConfig): void {
  if (config.version !== 2) return;
  if (config.connection?.transport !== undefined) {
    unsupported("/connection/transport", "connection.transport is not supported by the TypeScript SDK; use connection.http_client.");
  }
  const unsupportedTelemetry = Object.keys(config.telemetry ?? {}).filter(key => key !== "enabled");
  if (unsupportedTelemetry.length) {
    unsupported(`/telemetry/${escapePointer(unsupportedTelemetry[0]!)}`, `telemetry.${unsupportedTelemetry[0]} is not supported by the TypeScript SDK.`);
  }
  if (config.telemetry?.logging !== undefined) unsupported("/telemetry/logging", "telemetry.logging is not supported by the TypeScript SDK; use connection.debug_logger.");
  if (config.refresh !== undefined) unsupported("/refresh", "refresh is not supported by the TypeScript SDK because it has no local rule cache.");
  for (const [index, entry] of config.entrypoints.entries()) {
    if (typeof entry !== "string" && entry.turn?.finish && "driver" in entry.turn.finish && entry.turn.finish.driver !== undefined && entry.turn.finish.driver !== "generic") {
      unsupported(`/entrypoints/${index}/turn/finish/driver`, "TypeScript supports only the generic configured finish driver.");
    }
  }
  for (const [name, checkpoint] of Object.entries(config.checkpoints)) {
    if (checkpoint.input_from === "all_kwargs") {
      unsupported(`/checkpoints/${escapePointer(name)}/input_from`, "checkpoints.input_from: all_kwargs is supported only by the Python SDK.");
    }
  }
  for (const [name, adapter] of Object.entries(config.adapters ?? {})) {
    if (adapter.kind !== "openai_agents") unsupported(`/adapters/${escapePointer(name)}/kind`);
    if (adapter.runner !== undefined) unsupported(`/adapters/${escapePointer(name)}/runner`);
    for (const [toolName, tool] of Object.entries(adapter.tools ?? {})) {
      if ("tool" in tool) unsupported(`/adapters/${escapePointer(name)}/tools/${escapePointer(toolName)}/tool`);
    }
  }
}

/** Convert the shared v2 file fields to the existing TypeScript client options. */
export function resolveTypeScriptV2Connection(
  config: InstrumentationConfig,
  bindings: Record<string, object>,
  higherPrecedence: Record<string, unknown> = {},
): Record<string, unknown> {
  validateTypeScriptV2Capabilities(config);
  if (config.version !== 2) return {};
  const file = config.connection;
  const result: Record<string, unknown> = {};
  if (file?.runtime_url !== undefined) {
    if (typeof file.runtime_url === "string") result.runtimeUrl = file.runtime_url;
    else if (higherPrecedence.runtimeUrl === undefined) {
      const value = typeof process !== "undefined" ? process.env[file.runtime_url.env] : undefined;
      if (value !== undefined) result.runtimeUrl = value;
      else throw new TypeError(`Environment variable '${file.runtime_url.env}' required by instrumentation config is missing.`);
    }
  }
  if (file?.agent_key !== undefined) {
    if ("env" in file.agent_key) {
      if (higherPrecedence.agentKey === undefined) {
        const value = typeof process !== "undefined" ? process.env[file.agent_key.env] : undefined;
        if (value !== undefined) result.agentKey = value;
        else throw new TypeError(`Environment variable '${file.agent_key.env}' required by instrumentation config is missing.`);
      }
    } else result.agentKey = bindingValue(bindings, file.agent_key, "Agent Key");
  }
  if (result.agentKey !== undefined && typeof result.agentKey !== "string" && typeof result.agentKey !== "function") {
    throw new TypeError("connection.agent_key binding must resolve to a string or asynchronous key provider.");
  }
  if (file?.http_client !== undefined) {
    const client = bindingValue(bindings, file.http_client, "HTTP client");
    if (!(client instanceof HTTPClient)) throw new TypeError("connection.http_client binding must resolve to an HTTPClient.");
    result.httpClient = client;
  }
  if (file?.debug_logger !== undefined) {
    const logger = bindingValue(bindings, file.debug_logger, "Debug logger");
    if (!logger || typeof logger !== "object"
        || typeof (logger as { group?: unknown }).group !== "function"
        || typeof (logger as { groupEnd?: unknown }).groupEnd !== "function"
        || typeof (logger as { log?: unknown }).log !== "function") {
      throw new TypeError("connection.debug_logger binding must resolve to a Norma logger.");
    }
    result.debugLogger = logger;
  }
  if (file?.user_agent !== undefined) result.userAgent = file.user_agent;
  if (file?.timeout_ms !== undefined) result.timeoutMs = file.timeout_ms;
  if (file?.target_type !== undefined) result.targetType = file.target_type;
  if (file?.target_id !== undefined) result.targetId = file.target_id;
  if (file?.control_contract !== undefined) result.controlContract = normalizeControlContract(file.control_contract);
  if (config.telemetry?.enabled !== undefined) result.observabilityEnabled = config.telemetry.enabled;
  return result;
}

export function v2Config(config: InstrumentationConfig): InstrumentationConfigV2 | undefined {
  return config.version === 2 ? config : undefined;
}
