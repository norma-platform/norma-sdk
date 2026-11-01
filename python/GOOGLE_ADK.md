<!-- Modified for Norma. -->
# Google ADK

For a new application, use version 2 declarative configuration to bind the
entrypoint, existing agent identity, and ADK adapter at startup. This page's
explicit `norma.init()` and `NormaPlugin.bind()` flow remains an advanced
advanced path for integrations that need direct hook selection.

For example, bind a configured Runner to an application entrypoint without
serializing the Agent or callback objects:

```yaml
version: 2
entrypoints:
  - target: application.serve
adapters:
  support:
    kind: google_adk
    runner: {binding: adk.runner}
checkpoints: {}
```

Call `await norma.aconfigure("norma-agents", bindings=...)` once for the
application lifetime; normal requests reuse the configured entrypoint.

Download the supplied [Python wheel](https://norma-platform.cloud/artifacts/norma_sdk-8.7.1.dev18-py3-none-any.whl), save it in your project, then install the Google ADK extra: `python -m pip install './norma_sdk-8.7.1.dev18-py3-none-any.whl[google-adk]'`. The adapter dependency is `google-adk>=1.7.0,<3`.

Configure `NORMA_RUNTIME_URL` with your tenant HTTPS URL and `NORMA_AGENT_KEY`
with the scoped key issued in Console. Initialize before binding or running:

```python
import asyncio
import norma
from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from norma.integrations.google_adk import NormaPlugin

async def run(model):
    # Supply your configured ADK model; model inference has its own credentials/costs.
    norma.init("customer-service-agent")
    agent = Agent(name="assistant", model=model)
    plugin = NormaPlugin("customer-service-agent")
    plugin.bind(agent)  # Registers discovered model/tool steps; setup failures raise.
    sessions = InMemorySessionService()
    runner = Runner(agent=agent, app_name="service", session_service=sessions,
                    plugins=[plugin])
    try:
        await sessions.create_session(app_name="service", user_id="user", session_id="one")
        async for event in runner.run_async(
            user_id="user", session_id="one",
            new_message=types.Content(role="user", parts=[types.Part(text="Hello")]),
        ):
            print(event)
    finally:
        await plugin.close()
        await norma.ashutdown()

# asyncio.run(run(your_model))
```

The Runner `plugins` argument works across this matrix. ADK 2.12 deprecates it in
favor of passing an `App` containing the plugins; use that equivalent form when
building solely for newer ADK versions.

### Streamed finish on `Runner.run_async`

`Runner.run_async` is an async generator and can be configured as a version 2
entrypoint with `turn.finish.stream`. ADK yields Event objects, so select the text
from each current event with a bound provider; return `None` for events without
text and set `missing: skip`. The SDK passes every Event through unchanged and sends
one finish check after natural exhaustion. Closing the stream early does not run a
finish check. Streamed finishes do not automatically retry a held turn.

For a direct `runner.run_async(...)` call, bind the Runner and select identifiers
from the call arguments (or use your application's own provider for a fresh turn
ID):

```yaml
version: 2
entrypoints:
  - target: framework.runner.run_async
    turn:
      session_id: {path: [kwargs, session_id]}
      turn_id: {provider: callbacks.new_turn_id}
      finish:
        stream:
          text: {provider: callbacks.event_text}
          missing: skip
        on_hold: raise
adapters:
  adk:
    kind: google_adk
    runner: {binding: framework.runner}
checkpoints: {}
```

The event text provider receives an invocation whose `result` is the current ADK
Event. Use a provider that selects only text parts. An unavailable finish check
keeps the SDK's existing fail-open behavior; normal exhaustion does not prove a
Rule passed.

## Enforcement and recovery

Norma runs before and after model generation and tool calls. Binding discovers
steps; dynamically encountered steps must finish registration before checking and
execution. A plugin is bound to one successful Norma initialization; create and bind
a new plugin after reinitialization, even with the same agent name. Missing initialization, registration failure, context-extractor failure,
network failure, incompatible hosted responses and evaluator errors fail closed.
A context extractor must return the required evidence or raise; exceptions are
never replaced with empty context. Configure Norma **first** in the plugin list:
ADK stops processing callbacks after a plugin returns an override, so an earlier
plugin can bypass later Norma hooks. No adapter can enforce calls outside ADK's
selected lifecycle hooks.

A denied precheck replaces the model/tool response and prevents that selected
call. A steered precheck blocks the current generation and returns hosted guidance;
choose any revised proposal explicitly and submit it through a new check. Guidance
alone never grants permission. Tool steering likewise returns a blocked tool result
for the agent to handle. A postcheck happens after execution and cannot undo side
effects. Postcheck failures replace the result and do not replay the call.

`on_violation_callback(info, result)` receives the step/stage and public Rule
provenance: `rule_id`, `rule_name`, `check_id`, `stage`, `recovery_feedback`, plus
`action`, `message`, and `steering_context`. Feedback retains the normalized Rule
models. Callback failures are logged without changing the blocking result.

All four hooks are enabled by default (also for an empty set). A nonempty
`enabled_hooks` subset deliberately limits enforcement to the selected hooks:
`before_model`, `after_model`, `before_tool`, `after_tool`. Unknown names raise
instead of silently disabling protection. Disabling prechecks permits effects
before a postcheck; choose subsets only when that coverage is intentional.

## Reproduce verification

Run `make test-google-adk` from the Python SDK directory. It builds the wheel,
installs it and official PyPI dependencies into separate temporary environments,
then runs the copied test script outside the checkout. Override exact versions with
`make test-google-adk ADK_VERSIONS='1.7.0 2.12.0'`.

The script uses actual ADK Runner, Agent, BaseLlm, function tools, sessions and
callbacks. The model is deterministic and local; only Norma's HTTP transport is
mocked. Credential/export environment variables are cleared and a Python audit
hook rejects socket connect, DNS resolution and binding before framework imports.
No listeners, inference calls, hosted tenant requests or paid services are used.
The matrix covers allow, pre/post deny and steer, Rule/check provenance and feedback,
failed initialization/registration/context extraction, network and protocol failures,
embedded evaluator errors, post-action failure without replay, and configuration
validation. `make test-wheel` separately checks imports without optional packages.
