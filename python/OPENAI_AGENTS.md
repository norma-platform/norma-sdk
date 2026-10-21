<!-- Modified for Norma. -->
# OpenAI Agents function tools

Install `pip install 'norma-sdk[openai-agents]'`. This adapter is tested against
**openai-agents 0.22.3** on Python 3.12; its dependency range is `>=0.22.3,<0.23`.
Other patch releases in that range have not each been tested. Norma itself remains
importable without this optional dependency.

The adapter protects ordinary `FunctionTool` calls made by the official Runner.
It uses awaited native input guardrails plus an invocation-time comparison of the
checked JSON proposal, active Agent object and tool-call ID. It does not infer a
Norma identity from a model-produced name or the current global SDK session.

```python
from agents import Agent, Runner, function_tool
from norma import NormaClient
from norma.integrations.openai_agents import AgentBinding, protect_function_tool

@function_tool(failure_error_function=None)
async def send_message(recipient: str, text: str) -> str:
    # Put your application's authorized action here.
    return f"Message prepared for {recipient}"

async def run(model):
    agent = Agent(name="assistant", model=model)
    async with NormaClient() as client:
        binding = AgentBinding(agent=agent, client=client,
                               agent_name="customer-service-agent")
        await binding.initialize()
        agent.tools = [protect_function_tool(send_message, bindings=[binding])]
        return await Runner.run(agent, "Prepare a message")
```

Set `NORMA_RUNTIME_URL` to your tenant URL and `NORMA_AGENT_KEY` to the scoped key
issued in Console. Create the registered Norma agent there before initialization.
`binding.initialize()` verifies the registered identity and access through the
opened client; each tool call still requires a valid hosted-v1 response. A failed
initialization or a closed/reopened client blocks calls until initialization
succeeds again. The wrapper does not create credentials or agent identities, and
requires no global `norma.init()` session. Model credentials and inference costs
are separate from Norma configuration.

## Decisions, records and composition

- Allow invokes the selected tool once through the Runner's normal path.
- Deny, failed check, malformed responses, missing hosted proof, missing
  identity mapping and network errors raise the framework's native
  `ToolInputGuardrailTripwireTriggered` before that tool executes.
- Steer rejects that invocation and supplies redacted, bounded guidance through
  native `reject_content`. The model may propose a new call, which is checked
  independently. Norma never invokes the rejected call or retries it itself.
- `check_output=True` adds a native output guardrail. A postcheck cannot undo an
  effect already performed. It uses the original checked input and correlation,
  even if the tool subsequently changes its context.

The copied tool preserves its schema, approvals, existing guardrails and invocation
settings. Existing input guardrails run first. An approval is not permission from
Norma: execution after approval still runs the Rule check. Argument or identity
changes while a check is pending, or in later tool-start hooks, block invocation.
Application hooks must not themselves perform the protected side effect.

`ToolCheckRecord` is retained in native guardrail `output_info` and contains
`agent_name`, `framework_agent_name`, `tool_name`, `tool_call_id`, `stage`, the public
`Decision`, and a safe error type/optional `http_status` when no decision was available. Rule results use
`rule_id`, `rule_name` and `check_id`. This is a check record, **not proof of tool
execution or durable telemetry storage**. Retain native run results/tripwire output
according to your application's record policy. Call IDs are correlation supplied
by the framework/model, not an authorization identity.

For handoffs, bind every supported active Agent object explicitly. Missing mappings
fail closed, including another Agent with the same display name. The handoff
callback itself is outside this adapter's protected function-tool boundary.
Concurrent sibling calls have independent checks; denying one does not roll back
an already completed sibling. Streamed runs must be consumed to completion to
observe failures. Cancelling while a check is pending prevents that invocation;
raw model output may already have been emitted and cannot be recalled.

## Supported boundary and limitations

This version rejects built-in hosted tools, computer/shell/patch tools, agent-as-tool
continuations and programmatic callers. It makes no whole-agent or blanket handoff
coverage claim. Wrap nested agents' ordinary function tools separately and protect
other side-effecting seams with explicit checkpoints.

Native checks evaluate the proposed JSON **before** the tool's Pydantic coercion,
custom validators or other transformations. Do not assume a proposal check proves
invariants about a transformed value. Put a generic checkpoint inside the function
where those exact executed values are available. Likewise, direct Python calls
bypass native Runner guardrails and need a checkpoint. Set `step_type="tool"`
explicitly: this inner decorator sees the plain Python function before
`function_tool` wraps it, so automatic inference would classify it as an LLM step.

```python
import norma
from agents import function_tool

@function_tool(failure_error_function=None)
@norma.checkpoint(step_name="send_message", step_type="tool")
async def send_message(recipient: str, text: str) -> str:
    return f"Message prepared for {recipient}"

# Define checkpoints before initializing the generic session.
# norma.init(agent_name="customer-service-agent")
```

`context_extractor` can supply trusted JSON context; exceptions block the call.
The `norma` context key is reserved for framework/call correlation. Output checks
support JSON-compatible values; unsupported opaque output objects fail closed after
execution. Custom guardrails that perform side effects are application code and
are not protected merely by placing this wrapper around the tool.

## Verification

`make test-openai-agents` builds the wheel, installs it with official PyPI
openai-agents 0.22.3 and test dependencies in a temporary environment, copies the
test module outside the checkout, and runs the actual Runner with deterministic
models and in-memory tools. Only the Norma HTTP boundary is mocked. Provider
credentials are removed, tracing is disabled, and socket connections, DNS and
listening sockets are prohibited before framework imports. No inference, accounts,
new credentials or paid services are used.

Tests exercise allow/deny/steer, failed initialization, outages, malformed responses,
identity mapping and handoffs, approval resume, existing guardrails, parallel calls,
mutation during awaited checks and before invocation, output-check timing, and
streamed execution/cancellation. `make test-wheel` checks missing-optional-package
behavior separately. Actual private gateway/runtime tests are separate evidence;
the deterministic matrix does not claim to exercise the hosted backend.
