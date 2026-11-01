// Modified for Norma.
import type { ToolInputGuardrailDefinition, ToolOutputGuardrailDefinition } from "@openai/agents";
import type { NormaClient } from "./client";
import { resolveBinding } from "./instrumentation-v2";
import type { InstrumentationAdapter, InstrumentationOpenAIToolDefinition, BindingReference } from "./instrumentation";
import type { JsonObject, JsonValue } from "./types";
import type { NormaToolRequestContext } from "./openai-agents";
import type { InstrumentationNativeToolAction } from "./instrumentation";
import type { NormaAction } from "./checkpoint";
import { jsonValueSchema } from "./json-contract";
import { copyCheckpointArguments } from "./checkpoint-arguments";
import { selectorRootValue, type SelectorValueRoot } from "./instrumentation-selectors";

export interface AdapterRequestScope extends NormaToolRequestContext { context: JsonObject; active: boolean }
export interface PreparedAdapterInstall {
  agentName: string;
  nativeAgent: object;
  toolsArray: unknown[];
  originalTools: unknown[];
  additions: object[];
  verifyUnmodified(): void;
  install(): void;
  uninstall(): void;
}

function resolveCallback(bindings: Record<string, object>, reference: BindingReference, label: string): { owner: object; callback: (...args: unknown[]) => unknown } {
  const resolved = resolveBinding(bindings, reference.binding);
  if (typeof resolved.value !== "function") throw new TypeError(`${label} binding '${reference.binding}' must resolve to a callback.`);
  return { owner: resolved.owner, callback: resolved.value as (...args: unknown[]) => unknown };
}

function compileToolAction(
  action: InstrumentationNativeToolAction | undefined,
  bindings: Record<string, object>,
): ((args: unknown[], context: JsonObject, agentName: string) => Promise<NormaAction>) | undefined {
  if (!action) return undefined;
  const readers = ({} as Record<"target" | "command", (invocation: { args: unknown[]; kwargs: Record<string, never>; context: JsonObject; agent_name: string }) => Promise<JsonValue | string | null | undefined>>);
  for (const field of ["target", "command"] as const) {
    const source = action[field];
    if (source === undefined || source === null || typeof source === "string") {
      readers[field] = async () => source;
      continue;
    }
    if ("value" in source) {
      const snapshot = copyCheckpointArguments([jsonValueSchema.parse(source.value)], true)[0] as JsonValue;
      readers[field] = async () => copyCheckpointArguments([snapshot], true)[0] as JsonValue;
      continue;
    }
    if ("provider" in source) {
      const resolved = resolveBinding(bindings, source.provider);
      if (typeof resolved.value !== "function") throw new TypeError(`OpenAI action provider '${source.provider}' must resolve to a callback.`);
      readers[field] = async invocation => copyCheckpointArguments([
        jsonValueSchema.parse(await Reflect.apply(resolved.value as (...args: unknown[]) => unknown, resolved.owner, [invocation])),
      ], true)[0] as JsonValue;
      continue;
    }
    const [root, ...segments] = source.path;
    if (root === "result" || root === "error") throw new TypeError(`OpenAI tool action cannot select '${root}' before execution.`);
    if (root === "params") throw new TypeError("OpenAI tool actions cannot select the 'params' root; use 'input' for decoded tool arguments.");
    readers[field] = async invocation => {
      let current: unknown = selectorRootValue(root as SelectorValueRoot, invocation);
      for (const segment of segments) {
        if (!current || (typeof current !== "object" && typeof current !== "function")) throw new TypeError("OpenAI action value path is unavailable.");
        const descriptor = Object.getOwnPropertyDescriptor(current, String(segment));
        if (!descriptor || !Object.hasOwn(descriptor, "value")) throw new TypeError("OpenAI action value path is missing or uses an accessor.");
        current = descriptor.value;
      }
      return copyCheckpointArguments([jsonValueSchema.parse(current)], true)[0] as JsonValue;
    };
  }
  return async (args, context, agentName) => {
    const invocation = { args, kwargs: {} as Record<string, never>, context, agent_name: agentName };
    const [target, command] = await Promise.all([
      readers.target(invocation), readers.command(invocation),
    ]);
    const actionText = (value: JsonValue | string | null | undefined): string | null | undefined => {
      if (value !== undefined && value !== null && typeof value !== "string") {
        throw new TypeError("OpenAI tool action selectors must resolve to strings or null.");
      }
      return value;
    };
    return {
      kind: action.kind,
      ...(target === undefined ? {} : { target: actionText(target) }),
      ...(command === undefined ? {} : { command: actionText(command) }),
    };
  };
}

function resolveGuardrail<T>(bindings: Record<string, object>, reference: BindingReference, label: string, expectedType: "tool_input" | "tool_output"): T {
  const value = resolveBinding(bindings, reference.binding).value;
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError(`${label} binding '${reference.binding}' must resolve to a native guardrail object.`);
  const type = Object.getOwnPropertyDescriptor(value, "type")?.value;
  const name = Object.getOwnPropertyDescriptor(value, "name")?.value;
  const run = Object.getOwnPropertyDescriptor(value, "run")?.value;
  if (type !== expectedType || typeof name !== "string" || !name.trim() || typeof run !== "function") {
    throw new TypeError(`${label} binding '${reference.binding}' must be a valid native guardrail with own data fields type, name and run.`);
  }
  return value as T;
}

export async function prepareOpenAIAgentAdapter(
  agentName: string,
  adapter: Extract<InstrumentationAdapter, { kind: "openai_agents" }>,
  bindings: Record<string, object>,
  client: NormaClient,
  resolveScope: () => AdapterRequestScope,
): Promise<PreparedAdapterInstall> {
  if (adapter.runner) throw new TypeError("TypeScript OpenAI Agents adapters do not support runner interception; use the configured definition-form tools.");
  const native = resolveBinding(bindings, adapter.agent.binding).value;
  if (!native || typeof native !== "object" || Array.isArray(native)) throw new TypeError(`Adapter agent '${adapter.agent.binding}' must resolve to a native Agent object.`);
  const descriptor = Object.getOwnPropertyDescriptor(native, "tools");
  if (!descriptor || !Object.hasOwn(descriptor, "value") || descriptor.writable !== true
      || !Array.isArray(descriptor.value) || Object.getPrototypeOf(descriptor.value) !== Array.prototype
      || !Object.isExtensible(descriptor.value)) {
    throw new TypeError(`Adapter agent '${adapter.agent.binding}' must expose a writable own data-property tools array.`);
  }
  const toolsArray = descriptor.value as unknown[];
  const originalTools = [...toolsArray];
  const originalNames = new Set(originalTools.flatMap(tool => {
    if (!tool || typeof tool !== "object") return [];
    const name = Object.getOwnPropertyDescriptor(tool, "name")?.value;
    return typeof name === "string" ? [name] : [];
  }));
  const additions: object[] = [];
  const toolDefinitions = adapter.tools ?? {};
  const [{ normaTool }, { Agent }] = await Promise.all([import("./openai-agents"), import("@openai/agents")]);
  if (!(native instanceof Agent)) throw new TypeError(`Adapter agent '${adapter.agent.binding}' must be an instance of the installed @openai/agents Agent class.`);
  for (const [name, configured] of Object.entries(toolDefinitions)) {
    if (originalNames.has(name) || additions.some(item => Object.getOwnPropertyDescriptor(item, "name")?.value === name)) {
      throw new TypeError(`OpenAI Agent already has a tool named '${name}'; declarative installation will not replace ambiguous tools.`);
    }
    if ("tool" in configured) throw new TypeError("TypeScript OpenAI adapters require definition-form tools; native tool wrapping is Python-only.");
    const definition = configured as InstrumentationOpenAIToolDefinition;
    const execute = resolveCallback(bindings, definition.execute, `adapters OpenAI tool '${name}' execute`);
    const inputGuardrails = (definition.input_guardrails ?? []).map(reference => resolveGuardrail<ToolInputGuardrailDefinition<unknown>>(bindings, reference, `OpenAI tool '${name}' input guardrail`, "tool_input"));
    const outputGuardrails = (definition.output_guardrails ?? []).map(reference => resolveGuardrail<ToolOutputGuardrailDefinition<unknown>>(bindings, reference, `OpenAI tool '${name}' output guardrail`, "tool_output"));
    const onRecord = definition.on_record ? resolveCallback(bindings, definition.on_record, `OpenAI tool '${name}' on_record`) : undefined;
    const resolveAction = compileToolAction(definition.action, bindings);
    additions.push(normaTool({
      name,
      description: definition.description,
      parameters: definition.parameters as never,
      resolveClient: ({ agent }) => agent === native ? client : undefined,
      resolveRequestContext: () => resolveScope(),
      isActive: () => resolveScope().active,
      execute: (args, context, signal) => {
        if (!resolveScope().active) throw new TypeError("Configured Norma adapter is closed; execution was blocked.");
        return Reflect.apply(execute.callback, execute.owner, [args, context, signal]) as Promise<JsonValue>;
      },
      ...(resolveAction ? { resolveAction: (invocation: { args: unknown[]; kwargs: Record<string, never>; context: JsonObject; agent_name: string }) =>
        resolveAction(invocation.args, invocation.context, invocation.agent_name) } : {}),
      ...(definition.needs_approval === undefined ? {} : { needsApproval: definition.needs_approval }),
      ...(inputGuardrails.length ? { inputGuardrails } : {}),
      ...(outputGuardrails.length ? { outputGuardrails } : {}),
      onRecord: async record => {
        if (!resolveScope().active) throw new TypeError("Configured Norma adapter is closed; the check cannot be recorded.");
        if (onRecord) await Reflect.apply(onRecord.callback, onRecord.owner, [record]);
        if (!resolveScope().active) throw new TypeError("Configured Norma adapter closed while recording a check.");
      },
    }));
  }
  const verifyUnmodified = (): void => {
    const current = Object.getOwnPropertyDescriptor(native, "tools");
    if (current?.value !== toolsArray || current.writable !== true || toolsArray.length !== originalTools.length
        || originalTools.some((value, index) => toolsArray[index] !== value)) {
      throw new TypeError(`OpenAI Agent '${adapter.agent.binding}' tools changed during configuration validation.`);
    }
  };
  let installed = false;
  return {
    agentName, nativeAgent: native, toolsArray, originalTools, additions, verifyUnmodified,
    install() {
      verifyUnmodified();
      toolsArray.push(...additions);
      installed = true;
    },
    uninstall() {
      if (!installed) return;
      for (const addition of additions) {
        const index = toolsArray.indexOf(addition);
        if (index >= 0) toolsArray.splice(index, 1);
      }
      installed = false;
    },
  };
}
