# OpenAI Agents SDK

For a new application, use version 2 declarative configuration to bind existing
OpenAI Agent identities, entrypoints, and definition-form tools at startup. The
direct `NormaClient` and `normaTool` API below is an advanced path for explicit
guardrail composition. Install the [qualified SDK tarball](https://norma-platform.cloud/artifacts/norma-sdk-4.0.0-dev.15.tgz) and `@openai/agents@0.20.0` separately when using the
adapter. Declarative parsing uses the SDK's AJV/YAML dependencies and does not
load the optional OpenAI peer unless that adapter is configured.

```yaml
version: 2
entrypoints:
  - target: application.serve
adapters:
  support:
    kind: openai_agents
    agent: {binding: agents.support}
    tools:
      lookup_order:
        description: Look up an order
        parameters:
          type: object
          properties:
            orderId: {type: string}
          required: [orderId]
          additionalProperties: false
        execute: {binding: tools.lookup_order}
checkpoints: {}
```

Name the file for the exact registered Norma identity and bind real application
objects before calling `configure()`.

```ts
import { Agent, Runner } from "@openai/agents";
import { NormaClient } from "norma-sdk";
import { normaTool } from "norma-sdk/openai-agents";

const norma = new NormaClient();
norma.init({ agentName: "support-agent" }); // NORMA_AGENT_KEY + NORMA_RUNTIME_URL
const clients = new Map<Agent, NormaClient>();
const lookup = normaTool({
  name: "lookup_order",
  description: "Look up an order",
  parameters: {
    type: "object", properties: { orderId: { type: "string" } },
    required: ["orderId"], additionalProperties: false,
  },
  resolveClient: ({ agent }) => clients.get(agent),
  execute: async ({ orderId }, _context, signal) => {
    signal?.throwIfAborted();
    return { orderId, status: "example" };
  },
});
const agent = new Agent({ name: "Support", tools: [lookup] });
clients.set(agent, norma);
// Run with your application's model configuration and credentials.
const result = await new Runner().run(agent, "Look up order 123");
```

The resolver is an application-owned mapping from actual agent instances to initialized Norma clients. Never select keys, tenants, or agent identities from model arguments. The same tool can serve multiple agents if every active agent has an explicit mapping. Unmapped agents fail closed. `connectionRevision` changes before every `NormaClient.init` attempt, including failed attempts; an outstanding tool permission cannot survive reconfiguration.

`normaTool` accepts plain strict JSON object schemas and JSON outputs. Zod transformations, Standard Schema, binary outputs, and streaming tool outputs are not supported by this factory. Execute receives a detached JSON snapshot equal to the checked arguments. A direct `tool.invoke` without native guardrail permission fails. Keep using generic `checkpoint` around application functions when they are also called outside the Agents Runner.

Awaited native input guardrails check before effects. Allow proceeds once; deny, evaluator errors, missing hosted proof, malformed responses and transport failures stop execution. Pre-stage steer skips the tool and returns bounded, redacted corrective feedback to the model. Existing input/output guardrails are retained. Human approval remains required when `needsApproval: true`; checks run again when the approved call reaches execution. Post-stage steer stops the run with a tripwire rather than asking for an automatic retry. Post checks happen after effects: failure blocks the result, cannot undo the effect, and never automatically retries it. Parallel calls remain independent; a failure cannot roll back already executing siblings.

`onRecord` is an optional awaited callback carrying `agentName`, `frameworkAgentName`, `toolName`, `callId`, `stage`, `status` and the available `Decision`. A pre allowance is permission, not proof that an effect occurred. A post record is an SDK check after invocation, not independent verification of an external system. Keep these records only in application-approved storage; decisions and application exception traces may contain sensitive evidence. The adapter does not store credentials or create a hosted operation automatically.

Both streaming and ordinary runs use the same tool boundary. Always await `stream.completed`. Cancellation is checked immediately before invoking application code; code already running must cooperate with its supplied AbortSignal. Already emitted text cannot be recalled. Async lifecycle event listeners are observation hooks and are never used for authorization.

This adapter does not protect hosted MCP, web/file-search tools, built-in computer/shell/apply-patch tools, handoffs, tool-search orchestration, or arbitrary nested-agent tools. It does not attach itself to every agent automatically. Wrap application-controlled effects using `checkpoint`, or use a separately reviewed native authorization seam for those tool classes. An outer function wrapping a nested agent gates only entry; protect its inner tools separately. Handoff callbacks require their own enforcement before side effects.
