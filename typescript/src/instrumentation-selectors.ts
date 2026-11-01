// Modified for Norma.
export type SelectorValueRoot = "args" | "kwargs" | "result" | "error" | "context" | "input" | "params";

export interface SelectorInvocation {
  args: unknown[];
  kwargs: Record<string, unknown>;
  result?: unknown;
  error?: unknown;
  context: object;
}

export class SelectorValidationError extends TypeError {
  public readonly code = "selector" as const;
  public constructor(public readonly path: string) {
    super("Selector is incompatible with its target phase or declared parameters.");
    this.name = "SelectorValidationError";
  }
}

/** all_params is safe only when its positional arguments have explicit names. */
export function validateAllParamsParameterNames(config: import("./instrumentation").InstrumentationConfig): void {
  if (config.version !== 2) return;
  const pointerSegment = (value: string): string => value.replace(/~/g, "~0").replace(/\//g, "~1");
  for (const [name, item] of Object.entries(config.checkpoints)) {
    if (item.input_from === "all_params" && item.parameter_names === undefined) {
      throw new SelectorValidationError(`/checkpoints/${pointerSegment(name)}/input_from`);
    }
  }
}

/** Validate only known ValueSource slots; user payloads containing a `path` key remain data. */
export function validateInstrumentationSelectors(config: import("./instrumentation").InstrumentationConfig): void {
  const pointerSegment = (value: string): string => value.replace(/~/g, "~0").replace(/\//g, "~1");
  validateAllParamsParameterNames(config);
  type Source = import("./instrumentation").ValueSource | "all_kwargs" | "all_params";
  const check = (source: Source | undefined, names: readonly string[] | undefined, phase: "pre" | "post", pointer: string, forbiddenRoot?: string): void => {
    if (!source || typeof source !== "object" || !("path" in source)) return;
    const [root] = source.path;
    if ((phase === "pre" && (root === "result" || root === "error")) || root === forbiddenRoot) throw new SelectorValidationError(pointer);
    try { validateParameterSelector(source.path, names, "Value source"); } catch { throw new SelectorValidationError(pointer); }
  };
  const checkAction = (action: import("./instrumentation").InstrumentationAction | import("./instrumentation").InstrumentationNativeToolAction | undefined, names: readonly string[] | undefined, pointer: string): void => {
    if (!action) return;
    const fields = action as { target?: unknown; command?: unknown; cwd?: unknown };
    for (const key of ["target", "command", "cwd"] as const) {
      const value = fields[key];
      if (value && typeof value === "object" && "path" in value) check(value as Source, names, "pre", `${pointer}/${key}`);
    }
  };
  const entries = config.entrypoints;
  for (const [index, entry] of entries.entries()) {
    if (typeof entry === "string") continue;
    const base = `/entrypoints/${index}`;
    const names = entry.parameter_names;
    check(entry.context, names, "pre", `${base}/context`, "context");
    check(entry.target_scope?.type, names, "pre", `${base}/target_scope/type`);
    check(entry.target_scope?.id, names, "pre", `${base}/target_scope/id`);
    check(entry.trace?.trace_id, names, "pre", `${base}/trace/trace_id`);
    check(entry.trace?.span_id, names, "pre", `${base}/trace/span_id`);
    const turn = entry.turn;
    if (turn) {
      check(turn.session_id, names, "pre", `${base}/turn/session_id`);
      check(turn.turn_id, names, "pre", `${base}/turn/turn_id`);
      check(turn.user_request, names, "pre", `${base}/turn/user_request`);
      if (turn.finish && "final_message" in turn.finish) check(turn.finish.final_message, names, "post", `${base}/turn/finish/final_message`);
      if (turn.finish && "stream" in turn.finish) check(turn.finish.stream.text, names, "post", `${base}/turn/finish/stream/text`, "error");
    }
  }
  for (const [name, item] of Object.entries(config.checkpoints)) {
    const base = `/checkpoints/${pointerSegment(name)}`;
    for (const key of ["state_from", "input_from", "context_from", "tools_from", "ground_truth_from"] as const) check(item[key], item.parameter_names, "pre", `${base}/${key}`);
    check(item.output_from, item.parameter_names, "post", `${base}/output_from`);
    checkAction(item.action, item.parameter_names, `${base}/action`);
    if (item.options && typeof item.options === "object" && item.options.action && "kind" in item.options.action) checkAction(item.options.action as import("./instrumentation").InstrumentationAction, item.parameter_names, `${base}/options/action`);
  }
  if (config.version === 2) for (const [name, item] of Object.entries(config.operations ?? {})) {
    const base = `/operations/${pointerSegment(name)}`;
    for (const key of ["operation_key", "input", "context", "evidence", "tools"] as const) check(item[key], item.parameter_names, "pre", `${base}/${key}`);
    check(item.verification_session_id, item.parameter_names, "post", `${base}/verification_session_id`);
    checkAction(item.action, item.parameter_names, `${base}/action`);
    for (const [phase, observation] of Object.entries(item.observations ?? {})) {
      const phaseName = phase === "attempted" ? "pre" : "post";
      const unavailableRoot = phase === "succeeded" ? "error" : phase === "failed" ? "result" : undefined;
      check(observation?.event_key, item.parameter_names, phaseName, `${base}/observations/${phase}/event_key`, unavailableRoot);
      check(observation?.summary, item.parameter_names, phaseName, `${base}/observations/${phase}/summary`, unavailableRoot);
      check(observation?.external_reference, item.parameter_names, phaseName, `${base}/observations/${phase}/external_reference`, unavailableRoot);
    }
  }
  if (config.version === 2) for (const [adapterName, adapter] of Object.entries(config.adapters ?? {})) {
    if (adapter.kind !== "openai_agents") continue;
    for (const [toolName, tool] of Object.entries(adapter.tools ?? {})) {
      if ("action" in tool) checkAction(tool.action, undefined, `/adapters/${pointerSegment(adapterName)}/tools/${pointerSegment(toolName)}/action`);
    }
  }
}

/** Resolve a selector root without changing the provider's public invocation object. */
export function selectorRootValue(
  root: SelectorValueRoot,
  invocation: SelectorInvocation,
  parameterNames?: readonly string[],
): unknown {
  switch (root) {
    case "args": return invocation.args;
    case "kwargs": return invocation.kwargs;
    case "result": return invocation.result;
    case "error": return invocation.error;
    case "context": return invocation.context;
    case "input": return invocation.args[0];
    case "params": {
      if (parameterNames === undefined) throw new TypeError("Value source root 'params' requires parameter_names on its target declaration.");
      const params = Object.create(null) as Record<string, unknown>;
      for (let index = 0; index < parameterNames.length && index < invocation.args.length; index += 1) {
        Object.defineProperty(params, parameterNames[index]!, {
          value: invocation.args[index], enumerable: true, configurable: false, writable: false,
        });
      }
      return params;
    }
  }
}

/** Fail during startup when a configured selector lacks its explicit parameter metadata. */
export function validateParameterSelector(
  path: readonly (string | number)[],
  parameterNames: readonly string[] | undefined,
  label: string,
): void {
  if (path[0] !== "params") return;
  if (parameterNames === undefined) {
    throw new TypeError(`${label} requires parameter_names on its target declaration for the 'params' root.`);
  }
  const name = path[1];
  if (typeof name !== "string" || !parameterNames.includes(name)) {
    throw new TypeError(`${label} selects undeclared parameter '${String(name)}'.`);
  }
}
