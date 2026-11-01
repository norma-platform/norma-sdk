# Declarative configuration guides

This guide shows how to configure agent identity, request context, turn checks, and streamed finish checks with JSON or YAML. Each file describes one existing agent and uses the same configuration shape in both formats.

## Load one file per agent

Save files beneath `norma-agents/`; each lowercase file stem must exactly match an existing agent identity accessible to the configured key.

```yaml
version: 2
entrypoints:
  - target: application.support_chat
    context:
      provider: callbacks.request_context
    turn:
      session_id: { path: [input, session_id] }
      turn_id: { path: [input, turn_id] }
      user_request: { path: [input, message] }
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

Use the same values in JSON:

```json
{
  "version": 2,
  "entrypoints": [{
    "target": "application.support_chat",
    "context": { "provider": "callbacks.request_context" },
    "turn": {
      "session_id": { "path": ["input", "session_id"] },
      "turn_id": { "path": ["input", "turn_id"] },
      "user_request": { "path": ["input", "message"] },
      "finish": {
        "final_message": { "path": ["result", "answer"] },
        "on_hold": "raise"
      }
    }
  }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "step_type": "tool",
      "action": { "kind": "other", "target": { "path": ["input", "recipient"] } }
    }
  }
}
```

The JSON and YAML examples describe one configuration. Replace the application target and request selectors with values from your application. Use `input` to select Python's first bound user argument, excluding the method receiver, or TypeScript's first supplied positional argument. See the [configuration reference](configuration-reference.md#value-sources) for all selector roots and runtime limits.

## Bind objects and configure the SDK

Pass every root used by a target, provider, callback, or native option in the `bindings` map. Close the configured handle when application use ends.

```python
import asyncio
import norma

async def main():
    configured = await norma.aconfigure(
        "norma-agents",
        bindings={"application": application, "tools": tools, "callbacks": callbacks},
    )
    try:
        await application.support_chat(request)
    finally:
        receipts = await configured.aclose()

asyncio.run(main())
```

```ts
import { configure } from "norma-sdk";

const configured = await configure("norma-agents", {
  bindings: { application, tools, callbacks },
});
try {
  await application.support_chat(request);
} finally {
  const receipts = await configured.close();
}
```

Python also supports synchronous `norma.configure(...)`. A file path loads one file. A directory loads its immediate `.json`, `.yaml`, and `.yml` files in sorted order. In-memory configuration requires `agent_name` in Python or `agentName` in TypeScript. File and directory identity comes from the exact lowercase filename stem.

The startup sequence parses all files, checks runtime capabilities, resolves local bindings, and reads each existing agent and its enabled Rules before installing wrappers. It does not create agent identities or attach Rules. By default, startup reads schemas. Set `schemas.publish: true` to publish compatible Step schema updates for an existing agent.

## Map all Python keyword arguments into Check input

By default, a Python tool Check captures user arguments with defaults applied; the method receiver is excluded. Set `input_from: all_kwargs` only when the Check input should contain explicitly supplied keyword arguments alone; positional arguments and omitted defaults are excluded.

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    input_from: all_kwargs
```

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.run" }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "input_from": "all_kwargs"
    }
  }
}
```

This shorthand is Python-only. Without an input mapping, Python captures all signature-bound named tool arguments and applies defaults; `all_kwargs` deliberately captures only explicitly supplied keyword arguments. TypeScript tool Check input wraps positional values in `input.args`; an LLM Check input uses the raw positional array. Pass one request object and map it with `input_from: { path: [args, 0] }`, or use [named TypeScript arguments](configuration-guides.md#capture-named-typescript-arguments-into-one-check-input) for several positional slots. Run offline validation and strict Rule-path coverage before a live Check; unknown coverage is not proof.

## Capture named TypeScript arguments into one Check input

For a TypeScript checkpoint with several positional parameters, list their names in call order and set `input_from: all_params`. The SDK creates one object from the named slots supplied at the call. Omitted/default slots and extra unnamed arguments are excluded; names are declared explicitly and are not inferred from source text or rest parameters.

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    parameter_names: [recipient, subject, body]
    input_from: all_params
```

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.run" }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "parameter_names": ["recipient", "subject", "body"],
      "input_from": "all_params"
    }
  }
}
```

Save the YAML as `norma-agents/support-agent.yaml` or use the equivalent JSON tab. This example assumes the existing `support-agent` and `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` are configured. The program binds the application entrypoint and tool target, runs the configured function, and closes the handle. The target signature's defaulted `subject` and optional `body` are omitted from Check input when their positional slots are not supplied.

```ts
import { configure } from "norma-sdk";

async function main() {
  const tools = {
    async send_message(recipient: string, subject = "Balance", body?: string) {
      return `Sent ${subject} to ${recipient}: ${body ?? ""}`;
    },
  };
  const application = {
    async run() {
      return tools.send_message(
        "account-123",
        "Balance",
        "Check the current balance",
      );
    },
  };
  const configured = await configure("./norma-agents/support-agent.yaml", {
    bindings: { application, tools },
  });
  try {
    await application.run();
  } finally {
    await configured.close();
  }
}

await main();
```

With all three slots supplied, `step.input` is `{ "recipient": "account-123", "subject": "Balance", "body": "Check the current balance" }`. If the call supplies only `recipient`, the `subject` default and `body` are omitted from the object. Python rejects `all_params`; use `all_kwargs` for Python keyword arguments. The whole-root `params` path remains unsupported.

## Configure turn IDs and finish behavior

A turn selects stable session and turn IDs from each invocation. The following is an `entrypoints` fragment to place in a complete version 2 file. Generic finish checks select a message from the completed application result.

```yaml
entrypoints:
  - target: application.chat
    turn:
      session_id: { path: [input, session_id] }
      turn_id: { path: [input, turn_id] }
      user_request: { path: [input, message] }
      finish:
        final_message: { path: [result, answer] }
        on_hold: raise
```

`finish.on_hold: raise` raises `FinishHeldError` with the verdict and completed application result. Python exposes it as `error.original_result`; TypeScript exposes it as `error.result`. `on_hold: return` returns the result unchanged and requires an `on_verdict` callback. `continue_with` is an explicit application callback that can create a replacement result for another check. The SDK does not rerun the entrypoint.

Keep IDs stable for continuations of the same logical turn. Use a new turn ID for a new request and a new session ID for a new conversation. A post-return hold cannot undo application effects.

## Configure streamed finish checks

Use `finish.stream` for async-iterator entrypoints. The following is an `entrypoints` fragment to place in a complete version 2 file. The SDK selects text from each yielded item, accumulates it within a bound, and checks it after natural exhaustion.

```yaml
entrypoints:
  - target: application.stream_answer
    turn:
      session_id: { path: [input, session_id] }
      turn_id: { path: [input, turn_id] }
      finish:
        stream:
          text: { path: [result, text] }
          mode: concat
          max_chars: 8000
          missing: error
        on_hold: raise
```

`mode` accepts `concat` (default) or `last`. `max_chars` is an integer from 1 through 8000 and defaults to 8000. `missing` accepts `error` (default) or `skip`. Python supports synchronous and asynchronous generator entrypoints. TypeScript supports native async generators. For a regular function, declare `returns: "async_iterable"` for an `AsyncIterable` factory or `returns: "promise"` for a `Promise<AsyncIterable>` factory.

Chunks are forwarded as they are produced and cannot be recalled by a later finish decision. Early return, cancellation, consumer break, and an unhandled iterator error do not trigger finish. See the [streamed finish contract](streamed-finish.md) for lifecycle details.

## Configure request context and trace

An entrypoint can select context, target, and trace values for an invocation:

```yaml
entrypoints:
  - target: application.chat
    context: { provider: callbacks.request_context }
    target_scope:
      type: { value: agent }
      id: { value: support }
    trace:
      trace_id: { path: [input, trace_id] }
      span_id: { path: [input, span_id] }
```

For a standard Console Agent Key, omit `target_scope` or use the configured agent identity. Other targets require separate authorization and are rejected by standard Console Agent Keys. Context values are application metadata and do not grant authorization.

## Register a checkpoint state provider

Use `state_provider` when a checkpoint needs an application-owned JSON state snapshot. The value is an exact key in the flat `providers` registry passed to `configure()` or `aconfigure()`; dots in the key are literal and are not walked as a path.

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    step_type: tool
    state_provider: account.snapshot
```

The equivalent JSON configuration is:

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.run" }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "step_type": "tool",
      "state_provider": "account.snapshot"
    }
  }
}
```

Here `account.snapshot` is a flat registry key; its dot is literal. A missing or non-callable key fails configuration before a Check request or action. This is distinct from `bindings`, which resolves dotted paths such as `application.run` and `tools.send_message`; a ValueSource provider such as `payload.send_message` also resolves through `bindings`. Set `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` in the application process for the existing `support-agent` agent. These examples load `./norma-agents/support-agent.yaml` and close the configured handle in `finally`.

```python
import asyncio
from types import SimpleNamespace
import norma

async def main():
    async def send_message(*, recipient: str, subject: str, body: str) -> str:
        return f"Sent {subject} to {recipient}"

    def account_snapshot(*, recipient: str, subject: str, body: str) -> dict[str, str]:
        return {"recipient": recipient}

    tools = SimpleNamespace(send_message=send_message)

    async def run(request):
        return await tools.send_message(
            recipient=request["recipient"],
            subject=request["subject"],
            body=request["body"],
        )

    application = SimpleNamespace(run=run)
    configured = await norma.aconfigure(
        "./norma-agents/support-agent.yaml",
        bindings={"application": application, "tools": tools},
        providers={"account.snapshot": account_snapshot},
    )
    try:
        return await application.run({
            "recipient": "account-123",
            "subject": "Balance",
            "body": "Check the current balance",
        })
    finally:
        await configured.aclose()

asyncio.run(main())
```

```ts
import { configure } from "norma-sdk";

type Message = { recipient: string; subject: string; body: string };

async function main() {
  const tools = {
    async send_message(message: Message): Promise<string> {
      return `Sent ${message.subject} to ${message.recipient}`;
    },
  };
  const application = {
    async run(message: Message) {
      return tools.send_message(message);
    },
  };
  function accountSnapshot(...args: unknown[]) {
    const [value] = args;
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw new TypeError("Expected a message object");
    }
    const message = value as Record<string, unknown>;
    if (typeof message.recipient !== "string") {
      throw new TypeError("Expected recipient");
    }
    return { recipient: message.recipient };
  }

  const configured = await configure("./norma-agents/support-agent.yaml", {
    bindings: { application, tools },
    providers: { "account.snapshot": accountSnapshot },
  });
  try {
    return await application.run({
      recipient: "account-123",
      subject: "Balance",
      body: "Check the current balance",
    });
  } finally {
    await configured.close();
  }
}

await main();
```

Python calls the state provider with the checkpoint target's bound arguments and applied defaults. TypeScript passes the target's supplied positional arguments. Keep the provider signature aligned with the target. Each provider returns a JSON object captured before the check and reused as state after a successful call.

## Map Python keyword arguments into one Check input

For a Python checkpoint that should pass through every explicitly supplied keyword argument, use `input_from: all_kwargs` from the [Python shorthand above](configuration-guides.md#map-all-python-keyword-arguments-into-check-input). Use a ValueSource provider when the Check input needs selected, renamed, or nested fields. This configuration resolves `payload.send_message` through `bindings`; it does not use the state-provider registry.

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    step_type: tool
    input_from:
      provider: payload.send_message
```

The equivalent JSON is:

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.run" }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "step_type": "tool",
      "input_from": { "provider": "payload.send_message" }
    }
  }
}
```

This runnable Python setup calls the checkpoint with explicit keyword arguments and maps them into `step.input`:

```python
import asyncio
from types import SimpleNamespace
import norma

def build_send_message_input(invocation):
    values = invocation["kwargs"]
    return {
        "recipient": values["recipient"],
        "message": values["body"],
    }

async def main():
    async def send_message(*, recipient: str, subject: str, body: str) -> str:
        return f"Sent {subject} to {recipient}"

    tools = SimpleNamespace(send_message=send_message)

    async def run(request):
        return await tools.send_message(
            recipient=request["recipient"],
            subject=request["subject"],
            body=request["body"],
        )

    application = SimpleNamespace(run=run)
    configured = await norma.aconfigure(
        "./norma-agents/support-agent.yaml",
        bindings={
            "application": application,
            "tools": tools,
            "payload": {"send_message": build_send_message_input},
        },
    )
    try:
        return await application.run({
            "recipient": "account-123",
            "subject": "Balance",
            "body": "Check the current balance",
        })
    finally:
        await configured.aclose()

asyncio.run(main())
```

`invocation["kwargs"]` contains values explicitly passed by keyword; it does not include positional values or omitted defaults. The provider renames `body` to `message`, so `step.input` is `{"recipient":"account-123","message":"Check the current balance"}`. For a TypeScript target, pass one request object and select it with `input_from: { path: [args, 0] }`; see the [ValueSource reference](configuration-reference.md#value-sources). An unmapped TypeScript tool checkpoint wraps positional arguments in `input.args`; LLM checkpoints map the raw argument array.

## Next steps

- [Configuration reference](configuration-reference.md): field types, defaults, required conditions, accepted values, and runtime support.
- [Offline validation](configuration-validation.md): validate files without importing application code or contacting Norma.
- [Advanced SDK setup](configuration-advanced.md): use direct APIs for behavior that is executable code.
