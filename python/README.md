<!-- Modified for Norma. -->
# Norma Python SDK

A standalone hosted client for registration, Rule checkpoints, operation receipts,
finish checks, framework adapters and event delivery. It contains no policy engine,
local evaluator plugins, runtime service, deployment code or management console.

## Install and configure

Requires Python 3.12+. The SDK version is `norma-sdk==8.7.1.dev3`. Install its wheel:

```sh
python -m pip install 'https://norma-platform.cloud/artifacts/norma_sdk-8.7.1.dev3-py3-none-any.whl'
export NORMA_RUNTIME_URL="https://your-runtime.example/api/runtime"
export NORMA_AGENT_KEY="your-scoped-agent-key"
```

The alpha download endpoint is live.

Use the exact runtime URL and scoped key issued by Console. HTTPS is required,
except for loopback development. Agent identity is an explicit argument and is
independent of connection configuration. The hosted gateway authorizes the key,
agent and account; the SDK never chooses a tenant or sends a tenant override.

```python
import norma

@norma.checkpoint(step_name="send_message", step_type="tool")
def send_message(text: str) -> str:
    return your_application.send_message(text)

norma.init(agent_name="customer-service-agent")
try:
    send_message("Hello")
finally:
    receipt = norma.shutdown()
```

Define checkpoints before initialization so their schemas are registered. Rules are
created and attached in Console. Initialization errors must be resolved before a
checkpoint can execute. Registration permissions remain enforced by the gateway.
Explicit `runtime_url` and `agent_key` arguments override the two environment values.
There is no authentication-type setting or configurable credential header.

For pure authored-rule checks and the opt-in Console controls receipt/evidence flow, see the [controls guide](../docs/controls.md). The controls option only negotiates read/check projections; authoring and changing per-agent values remain Console operations.

For explicit execution decisions:

```python
from norma import NormaClient, Step, checks

async def proposed_action():
    async with NormaClient() as client:
        decision = await checks.check(
            client, "customer-service-agent",
            Step(type="tool", name="send_message", input={"text": "Hello"}), "pre",
        )
        if decision.allowed:
            return your_application.send_message("Hello")
```

## Recover from a schema conflict

A registration error with HTTP `409`, `SCHEMA_INCOMPATIBLE` and a step schema
mismatch means the proposed input/output schemas conflict with saved step schemas.
The rejected request does not save or stage a proposal in Console. Keep startup stopped and export the
complete proposed steps array from your code.

Save this as `export_step_schemas.py` in your project. Replace the example import
with **all modules defining this agent's checkpoints**. Those modules must only
define functions at import time: keep `norma.init()`, network calls and application
execution in your startup function or `if __name__ == "__main__":` block.

```python
import json
from pathlib import Path

import norma
from norma.models import StepSchema
from my_application import checkpoints  # Replace with your checkpoint modules.

# If startup passes init(steps=...), use that same list here instead of [].
explicit_steps = []
explicit_schemas = [StepSchema.model_validate(step) for step in explicit_steps]
explicit_keys = {(schema.type, schema.name) for schema in explicit_schemas}
schemas = explicit_schemas + [
    schema for schema in norma.checkpoint_schemas()
    if (schema.type, schema.name) not in explicit_keys
]
Path("norma-step-schemas.json").write_text(
    json.dumps([
        schema.model_dump(mode="json", exclude_none=True) for schema in schemas
    ], indent=2) + "\n",
    encoding="utf-8",
)
```

Run `python export_step_schemas.py` with the SDK installed. Collecting schemas
needs no credentials, `norma.init()`, network request or checkpoint execution.
Explicit steps take precedence over decorated steps with the same `(type, name)`,
just as at initialization. Include every step that must remain registered, even
when only one changed.

As the workspace owner, open **Agents → your agent → Connection → Edit step
schemas**. The editor initially contains the **saved** schemas. Choose **Import
schemas** and select `norma-step-schemas.json`, or paste its array into the editor.
For one change, you can instead import or paste a single complete step object.
Console adds or replaces the step with that `(type, name)` and preserves the other
steps. Use the complete SDK array when reconciling all startup schemas; an array
replaces the full list, so include every step that must remain registered.

Review the diff and affected Rules/callers, confirm any removals, then choose
**Save changes**. Importing only changes the draft. Attached Rules remain in place.

After saving, retry your original `norma.init(...)` call with `conflict_mode="strict"`,
preserving `steps=...` if supplied and all other startup options. Use the same scoped
Agent Key and the same schema definitions you exported. Strict comparisons use the
complete input/output schema objects. Do not automatically retry with
`overwrite` or a stronger key. If another owner updates the registration during
review, reload and review against the latest saved version.

## Hosted execution contract

Every check sends `execution_mode: "hosted"`. The runtime must return
`execution_contract: "hosted-v1"`, confirming it rejected unsupported local Rules
against the same effective Rule snapshot it evaluated. Missing or unknown proof,
malformed decisions, authentication failures and network failures block checkpoint
execution. There is no fallback to local check and no separate check-then-evaluate
preflight race in the client.

A pre-stage failure prevents the action. A post-stage failure blocks the returned
result after the action already ran; it cannot roll back the action. The SDK never
retries the user action. Low-level `checks.check` returns a decision: its caller must
inspect `allowed` before executing. `operations.create` records a proposed tool action,
requires blocking error/unresolved policies and does not execute the tool.

Finish checks have a distinct completion contract: `check_finish` returns an explicit
`unavailable` / `finished_unverified` result when confirmation is unavailable. It may
allow the turn to finish, but never claims a verified result or executes an action.
Event delivery failures cannot change a Rule decision. Inspect flush/shutdown receipts
for pending, dropped or unconfirmed events rather than assuming persistence.

## Optional adapters

Extras are `strands-agents`, `google-adk`, `pydantic-ai`, `openai-agents`, and `otel`.
Install only the adapters your application uses. Strands exposes `NormaPlugin` and
`NormaSteeringHandler`; Google ADK exposes `NormaPlugin`. Finish adapters retain
completed tool history when asking a model to revise its final answer.

## Development

From this directory, without a monorepo checkout:

```sh
make sync
make check
make build
make test-wheel
```

`make sync` installs development and optional framework test dependencies from
`uv.lock` with frozen resolution and dependency source builds disabled. It then
builds only this local SDK for editable development. Update dependencies explicitly
with `uv lock`, review the lockfile, and rerun qualification. `make check` runs lint, strict function typing and isolated tests.
Tests use mocked transports and framework runners; they start no services. The wheel
check installs only the built artifact and its runtime dependencies into a temporary
environment outside the repository. The `norma` command is included.

### Hosted diagnostics

- `norma config check` validates the required environment variables offline.
- `norma doctor --agent my-registered-agent` performs one authenticated agent read.
- `norma version` prints the installed SDK version.

Diagnostics do not print keys or URLs, save credentials, create resources, or start services. Doctor confirms agent access only; it does not verify check compatibility or tenant-host binding.

See LICENSE and NOTICE for retained attribution.

Public Decisions expose `allowed`, `reason`, and `checks`. Each check has `rule_id`,
`rule_name`, optional `check_id`, and a status of `matched`, `not_matched`, `error`,
or `unresolved`. `model_dump()` and JSON schemas use those same public names.
Use `evaluate_rules` for initialized session checks and `refresh_rules` or
`refresh_rules_async` to refresh hosted Rule configuration. Rule exceptions accept
`rule_id`, `rule_name`, and `check_id`; failed checks expose `check_ids`.

Every checkpoint requires successful initialization, including its private
implementation. An explicit blocked runtime decision remains blocked even if its
individual results are advisory. Operation receipts expose `decision.allowed`,
`decision.checks`, and `rules`.

A retained operation's `outcome` records its original policy result. Operations use
their retained policy when deriving `decision.allowed`; an explicitly allowed
operation can remain allowed with advisory checks. Contradictory outcomes are
rejected.

For the real Google ADK Runner integration, setup and enforcement semantics, see
[Google ADK](GOOGLE_ADK.md). `make test-google-adk` verifies the installed wheel with
real ADK 1.7.0 and 2.12.0, deterministic local models/tools and mocked hosted HTTP;
network operations are prohibited during execution.

[OpenAI Agents function tools](OPENAI_AGENTS.md) documents the optional native
Rule guardrails, explicit active-agent bindings, preserved approvals and supported
boundaries. `make test-openai-agents` verifies the installed wheel with the official
Runner and deterministic local models/tools.

### Observe application state at a checkpoint

Pass an optional small JSON object as `state`, or a synchronous/asynchronous
callback accepting the action's arguments. The SDK captures the object once per
invocation before the precheck. Rules can read it at `context.state`. Both pre
and post checks use that same observation; a postcheck does not reload state.

```python
import norma

# Register this checkpoint before norma.init(). The application owns the facts.
@norma.checkpoint(step_name="reserve_inventory", step_type="tool", state={"available": 12})
async def reserve_inventory(quantity: int):
    return await inventory.reserve_atomically(quantity)
```

In the Console, a Rego condition can select this whole checkpoint and use the
explicit checkpoint view. The existing Rule's action decides what a match does:

```json
{
  "selector": {"path": "*"},
  "evaluator": {
    "name": "rego",
    "config": {
      "input_view": "checkpoint-v1",
      "query": "input.args.quantity > input.state.available"
    }
  }
}
```

This view exposes named Python arguments as `input.args` and the observation as
`input.state`; it does not change the Step wire format. Optional `input_schema`
validates this adapted object and can require the fields/types your Rule needs.
Absent state becomes `{}`. Existing Rego Rules that omit `input_view` keep the
selected payload unchanged (`input.input` and `input.context.state` for a whole
Step). An undefined query does not match; require critical data with the schema.

For user-specific values, supply a callback that uses your application's trusted
request context and authorizes the entity lookup. For example,
`state=inventory_snapshot` may return `{"available": 12, "order_id": order_id}`.
It receives the action's arguments, including applied defaults, on a separate
copy; it should be side-effect free. There is no SDK global “current user” state
and no mandatory database fetch or snapshot registry. A fresh invocation captures
fresh values, and concurrent invocations retain separate snapshots. Do not mutate
one shared state object to switch between users; use the callback instead.

State must be a plain JSON object: string keys, JSON scalars, lists, and objects.
Application instances, serialization hooks, tuples, cycles, nonfinite numbers,
negative zero, and integers outside ±(2^53−1) are rejected. Limits are 65,536
UTF-8 bytes of compact JSON, 32 levels of nesting (root at zero), and 10,000
values/containers/keys. Fractional JSON numbers retain ordinary floating-point
precision; use integer minor units for money. Select only intended facts; the SDK
does not serialize whole application objects or fetch credentials automatically.
An invalid snapshot or failing callback raises `CheckError(stage="pre")` before
the action executes; the exception message omits callback error details.

State is sent for evaluation whether or not retention is enabled. Saving the
exact Check payload for later inspection/replay is controlled separately by the
workspace's retention settings. A state field claiming an owner or tenant does
not authenticate it: workspace scope comes from the Agent Key, while application
code remains responsible for entity ownership. Snapshots are observations, not
locks, reservations, or Norma-managed balances. Concurrent updates still require
an atomic application transaction. Do not retry an action automatically after a
postcheck failure; the effect has already occurred.
