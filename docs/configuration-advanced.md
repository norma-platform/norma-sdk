# Advanced SDK setup

Use configuration for data mappings and declared settings. Put behavior that needs executable code in application setup and bind its objects by name. See [Configuration reference](configuration-reference.md) for exact field support.

| Need | Configuration | Application code |
| --- | --- | --- |
| Select request values or map state/action evidence | ValueSource and mapping fields | Bind a provider for application-owned computation. |
| Resolve a TypeScript client or request context for each OpenAI tool | Bind one client to the configured Agent and set entrypoint context | Use `normaTool` resolvers per tool. |
| Use Strands steering behavior | Configure documented Strands adapter hooks | Add `NormaSteeringHandler` as a Strands Agent plugin. |
| Use a custom Python telemetry sink | Select registered sink name and JSON settings | Register a factory for an application-owned RuleEventSink. |
| Own proposal, decision, and execution | Use a checkpoint where its function boundary fits | Call the explicit check API and branch before execution. |

## Resolve a TypeScript client per tool

The configured OpenAI adapter uses one client for a bound Agent. A direct `normaTool` can resolve a client and request context from each native tool invocation:

```ts
import { type ConfiguredAgents } from "norma-sdk";
import { normaTool } from "norma-sdk/openai-agents";

export function createLookupTool(configured: ConfiguredAgents) {
  const lookup = normaTool({
    name: "lookup_order",
    description: "Look up an order",
    parameters: {
      type: "object",
      properties: { orderId: { type: "string" } },
      required: ["orderId"],
      additionalProperties: false,
    },
    resolveClient: ({ agent }) => agent.name === "Support"
      ? configured.clients["support"]
      : undefined,
    resolveRequestContext: () => ({ context: { source: "support-workflow" } }),
    execute: async ({ orderId }, _context, signal) => {
      signal?.throwIfAborted();
      return { orderId, status: "example" };
    },
  });
  return lookup;
}
```

Pass the `ConfiguredAgents` handle created by the application and use the exact configured identity key. Never derive credentials or identities from model arguments. `normaTool` requires a strict JSON object schema and returns JSON values; it does not instrument hosted tools or agent handoffs.

## Add Strands steering

The direct `NormaSteeringHandler` calls `norma.evaluate_rules` through the active SDK state. Configure an application entrypoint that calls the Agent, then invoke that wrapped entrypoint so the handler uses its configured session:

```yaml
version: 2
entrypoints:
  - target: application.answer
checkpoints: {}
```

The equivalent JSON is:

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.answer" }],
  "checkpoints": {}
}
```

```python
import norma
from strands import Agent
from norma.integrations.strands import NormaSteeringHandler

async def answer_with_norma(model, tools, request):
    handler = NormaSteeringHandler(agent_name="support")
    agent = Agent(model=model, tools=tools, plugins=[handler])

    class Application:
        async def answer(self, request):
            return await agent.invoke_async(request)

    application = Application()
    configured = await norma.aconfigure(
        "norma-agents", bindings={"application": application}
    )
    try:
        return await application.answer(request)
    finally:
        await configured.aclose()
```

Call `answer_with_norma` with the application's model, tool list, and request. The config file targets `application.answer`; the wrapper establishes configured SDK state for the Agent plugin call. Do not attach a second adapter to the same Agent boundary.

## Register a Python sink factory

Implement a RuleEventSink and register a factory before loading configuration. The factory below receives an application-owned sink and returns it; the configuration's sink name selects that registered factory.

```python
import norma
from norma.observability import BaseRuleEventSink, register_rule_event_sink_factory

async def configure_agents(application, sink: BaseRuleEventSink):
    register_rule_event_sink_factory("team", lambda _config: sink)
    return await norma.aconfigure(
        "norma-agents",
        bindings={"application": application},
    )
```

```yaml
version: 2
telemetry:
  enabled: true
  sink_name: team
  sink_config: {}
entrypoints:
  - target: application.answer
checkpoints: {}
```

```json
{
  "version": 2,
  "telemetry": { "enabled": true, "sink_name": "team", "sink_config": {} },
  "entrypoints": [{ "target": "application.answer" }],
  "checkpoints": {}
}
```

The sink owns its delivery queue, batching, and retries. Keep credentials in application settings. TypeScript configuration accepts `telemetry.enabled` only.

## Use an explicit check

Call an explicit check when application code owns the proposal and execution branch. The [Explicit checks guide](/docs/sdk-explicit-checks) provides complete Python and TypeScript examples that handle check errors, deny, steer, and allow before executing. A check request does not run the action.

## Keep setup on one boundary

Use either a configured checkpoint for a function slot or a direct check inside the workflow that owns execution. Do not place both wrappers around the same function slot. When a native framework tool has a direct resolver or lifecycle API that configuration does not expose, use the direct SDK integration on that boundary.
