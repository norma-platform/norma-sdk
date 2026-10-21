<!-- Modified for Norma. -->
# Google ADK

Install `pip install 'norma-sdk[google-adk]'`. The supported dependency range is
Google ADK `>=1.7.0,<3`; plugins first appeared in 1.7.0. The real framework matrix
currently verifies **1.7.0 and 2.12.0** on Python 3.12. Intermediate releases have
not each been tested. ADK remains optional: importing Norma without it works, and
accessing `NormaPlugin` explains how to install the extra.

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
call. A steered precheck also blocks the call and returns the hosted guidance.
**Migration:** model steering no longer injects instructions and silently continues
generation. Choose a revised proposal explicitly and submit it through a new check;
guidance alone never grants permission. Tool steering likewise returns a blocked
tool result for the agent to handle. A postcheck happens after execution and cannot
undo side effects. Postcheck failures replace the result and do not replay the call.

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
