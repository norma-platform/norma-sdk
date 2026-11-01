# Configure agents at application boundaries

Declarative instrumentation binds existing agent identities to application entrypoints and checkpoint targets. Configuration selects values and settings; bound application objects supply functions, providers, callbacks, and framework instances.

## Configure identity and targets

Each JSON or YAML file belongs to one existing agent. Its exact lowercase filename stem is the identity. A document requires `version: 2`, a nonempty `entrypoints` list, and a `checkpoints` mapping. The configured SDK reads each existing agent and its enabled Rules before installing wrappers. It does not create identities or attach Rules.

Entrypoints establish identity, request context, target scope, trace, and turn settings. An entrypoint target belongs to one configured identity. Checkpoints bind functions by stable step name; multiple agent files can share a checkpoint target, and the active entrypoint scope selects its configuration. A configured target called without active identity scope fails.

```yaml
version: 2
entrypoints:
  - target: application.support_chat
    context: { provider: callbacks.request_context }
    turn:
      session_id: { path: [input, session_id] }
      turn_id: { path: [input, turn_id] }
      finish:
        final_message: { path: [result, answer] }
        on_hold: raise
checkpoints:
  send_message:
    target: tools.send_message
    step_type: tool
    action:
      kind: other
      target: { path: [input, recipient] }
```

Bind real application objects before calling `configure()`:

```ts
const configured = await configure("norma-agents", {
  bindings: { application, tools, callbacks },
});
try {
  await application.support_chat(request);
} finally {
  await configured.close();
}
```

Python uses `norma.configure(...)` or `norma.aconfigure(...)`; TypeScript uses async `configure(...)`. Directory input loads immediate `.json`, `.yaml`, and `.yml` files in sorted order. In-memory config accepts an explicit `agent_name` in Python or `agentName` in TypeScript.

## Preserve application-owned execution

The SDK wraps selected boundaries and restores only slots it still owns during close. Call through the configured mutable object property. Function aliases captured before configuration continue to reference the original function. TypeScript accepts mutable data-property slots; it does not mutate ESM namespace exports or call accessors to guess values.

Providers and callbacks are application code referenced by `{binding: ...}` or `{provider: ...}`. Configuration does not evaluate code strings, import modules, invoke getters, or add serialized callback bodies. A missing selected path or unavailable binding is an error.

## Select request input

Version 2 ValueSource roots include `input` and `params`, in addition to `args`, `kwargs`, `result`, `error`, and `context`. Python resolves `input` from the first signature-bound user parameter and `params` from named bound parameters. TypeScript selects the first supplied positional value for `input`; `params` requires declared `parameter_names` on the same target. Native OpenAI tool input is the decoded object and uses `input`.

For example, TypeScript positional names make a request selector explicit:

```yaml
entrypoints:
  - target: application.handle
    parameter_names: [request, options]
    turn:
      session_id: { path: [params, request, session_id] }
      turn_id: { path: [input, turn_id] }
```

The TypeScript runtime does not inspect function source or length to infer names. An omitted positional argument stays missing even if JavaScript would apply a default inside the function.

## Check turn completion and streams

Generic finish checks run after the application result exists. `on_hold: raise` raises `FinishHeldError`; `on_hold: return` requires `on_verdict` and returns the result unchanged. An explicit `continue_with` callback can create a replacement result for another check, but the application entrypoint is not rerun.

For async-iterator entrypoints, `finish.stream` collects selected text and checks it after natural exhaustion. Chunks are forwarded before the verdict and cannot be recalled. Python supports sync and async generators. TypeScript requires an async generator or a declared `AsyncIterable`/`Promise<AsyncIterable>` return. See [streamed finish](streamed-finish.md).

## Use configuration fields by runtime

The shared schema defines document structure. Each SDK also validates whether it can consume a field. Python supports telemetry, logging, OpenTelemetry, Rule refresh, and its documented native framework adapters. TypeScript supports `telemetry.enabled`, its connection client and logger settings, and the definition-form OpenAI Agents adapter; it rejects Python-only settings.

See the [configuration reference](configuration-reference.md), [configuration guides](configuration-guides.md), [offline validation](configuration-validation.md), and [advanced SDK setup](configuration-advanced.md) for field details and direct-API recipes.
