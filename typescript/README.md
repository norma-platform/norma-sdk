# Norma TypeScript SDK

A typed client for the hosted Norma runtime. Requires Node.js 20 or later.

```sh
npm install --ignore-scripts 'https://norma-platform.cloud/artifacts/norma-sdk-4.0.0-dev.2.tgz'
export NORMA_RUNTIME_URL=https://your-tenant.example/api/runtime
export NORMA_AGENT_KEY=your-agent-key
```

The alpha SDK is `norma-sdk@4.0.0-dev.2`.
This package has not been published to a registry.

Use the runtime URL from your Norma connection settings, including its path prefix. The Agent Key is sent in `X-API-Key`. HTTPS is required except for loopback development. Redirects are rejected. There is no auth-mode or tenant-ID setting.

```ts
import { NormaClient, checkpoint } from "norma-sdk";

const client = new NormaClient();
client.init({ agentName: "support-agent" });

// Registration requires this application name to be authorized by the key.
await client.agents.register({
  agent: { agentName: "support-agent" },
  steps: [{ type: "tool", name: "send_message", inputSchema: { type: "object" } }],
});

const send = checkpoint(async (message: string) => {
  return sendMessage(message); // your application function
}, { client, stepName: "send_message" });
await send("Hello");
await client.flushObservability();
```

`agentName` identifies your application agent, not a customer account. `runtimeUrl` and `agentKey` may be provided explicitly to `init` instead of the two environment variables. Keep keys on a trusted server. A custom `HTTPClient` is an advanced trusted transport boundary.

The runtime must support `hosted-v1`: every check sends `execution_mode: "hosted"` and requires `execution_contract: "hosted-v1"` in the response. Missing proof fails closed; an older runtime must be upgraded. The runtime must reject local-only rules from the same effective-rule snapshot used to evaluate.

For pure authored-rule checks and the opt-in Console controls receipt/evidence flow, see the [controls guide](../docs/controls.md). The controls option only negotiates read/check projections; authoring and changing per-agent values remain Console operations.

`checkpoint` evaluates before and after one invocation. A denied or failed precheck prevents the function from running. A failed postcheck raises `CheckError` after the function has run; do not automatically replay that action. `RuleViolationError` and `RuleSteerError` expose rule and check IDs. `checkFinish` and `TurnRecorder` support bounded final-turn checks and action recording.

## Recover from a schema conflict

A registration error with HTTP `409`, `SCHEMA_INCOMPATIBLE` and a step schema
mismatch means the proposed input/output schemas conflict with saved step schemas.
The rejected request does not save or stage a proposal in Console. Keep startup stopped and export the exact
complete `steps` array supplied to `client.agents.register`.

Move that array into an `agent-steps.ts` module as an exported constant named
`steps`, preserving every schema. Import the same array in your registration code
and in this separate `export-step-schemas.ts` script. The definitions module must
not initialize a client, make network requests or execute application work on import.

```ts
import { writeFileSync } from "node:fs";
import { steps } from "./agent-steps.js";

writeFileSync("norma-step-schemas.json", JSON.stringify(steps, null, 2) + "\n", "utf8");
```

Compile/run the export script with your project's TypeScript setup. It needs no
credentials, client initialization, registration request or checkpoint execution.
Console accepts the SDK's `inputSchema`/`outputSchema` fields as well as the JSON
wire format's `input_schema`/`output_schema`; do not include both spellings on the
same step or rename fields inside the schemas.

As the workspace owner, open **Agents → your agent → Connection → Edit step
schemas**. The editor initially contains the **saved** schemas. Choose **Import
schemas** and select `norma-step-schemas.json`, or paste its array into the editor.
For one change, you can instead import or paste a single complete step object.
Console adds or replaces the step with that `(type, name)` and preserves the other
steps. Use the complete SDK array when reconciling all startup schemas; an array
replaces the full list, so include every step that must remain registered.

Review the diff and affected Rules/callers, confirm any removals, then choose
**Save changes**. Importing only changes the draft. Attached Rules remain in place.

Retry registration with the original scoped Agent Key and the same definitions:

```ts
await client.agents.register({
  agent: { agentName: "your-agent-name" },
  steps,
  conflictMode: "strict",
});
```

Strict comparisons use the complete input/output schema objects. Do not
automatically retry with `overwrite` or a stronger key. If another owner updates
the registration during review, reload and review against the latest saved version.

### Observed state

Pass a small explicit JSON object when values are already available, or a synchronous/asynchronous
`state` callback when each invocation needs its own values:

```ts
const reserve = checkpoint(
  async (orderId: string, quantity: number) => reserveAtomically(orderId, quantity),
  {
    client,
    stepName: "reserve_inventory",
    state: async (orderId, _quantity) => {
      // Application code must authorize the entity for the authenticated request.
      const order = await loadAuthorizedOrder(orderId);
      return { order: { id: orderId, available: order.available } };
    },
  },
);
await reserve("order-123", 2);

// For values already available in a trusted request handler:
const sendReport = checkpoint(sendReportFile, {
  client, state: { plan: "standard", remainingBytes: 4096 },
});
```

The SDK captures state once at invocation, before the precheck, and places the copied values in
`step.context.state`. Both precheck and postcheck use that same snapshot; the postcheck does not
fetch current state again. A new invocation captures new values. Generic tool arguments remain
`step.input.args`. State adds no SDK environment variables,
global current-user value, or required metadata/schema registration.

In Console, this rule denies a reservation when its quantity exceeds the observed
availability. Rego still needs a selector; here it selects the whole checkpoint
with an explicit input view:

```json
{
  "scope": {
    "step_types": ["tool"],
    "step_name_regex": "^reserve_inventory$",
    "stages": ["pre"]
  },
  "condition": {
    "selector": {"path": "*"},
    "evaluator": {
      "name": "rego",
      "config": {
        "input_view": "checkpoint-v1",
        "query": "input.args[1] > input.state.order.available"
      }
    }
  },
  "action": {"decision": "deny"}
}
```

The SDK marks positional arguments explicitly, so this view exposes `input.args`
as an array and `input.state` as the observed object. Optional `input_schema`
validates that adapted object and can require critical fields/types. Missing
state becomes `{}`; an undefined query does not match. The Step wire shape stays
unchanged. Existing Rules without `input_view` retain the selected payload
(`input.input.args` and `input.context.state` for a whole Step); older SDKs without
the positional marker should keep that view until upgraded.

Snapshots accept plain JSON objects, nested objects/arrays, strings, booleans, null, and finite
numbers. Integers must be within ±(2^53−1); negative zero is rejected. Limits are 65,536 serialized
UTF-8 bytes, depth 32 (root depth zero), and 10,000 nodes counting values/containers and object keys.
Cycles, sparse arrays, classes, getters, `toJSON` functions, and other non-JSON values are rejected.
Select only the facts needed by Rules; do not pass entire application objects or secrets.
Callback arguments are immutable copies. Callback or state-validation failure raises `CheckError`
with `stage: "pre"` before any action; callback exception text is not included.

State is sent for evaluation regardless of retention settings. Retaining the evaluated snapshot
requires the workspace's supported capture/retention configuration; supplying state alone does not
enable retention. Authentication scopes the workspace through the Agent Key, while the application
remains responsible for authenticating users and authorizing entity lookups. Concurrent invocations
have independent snapshots, but snapshots do not reserve inventory, lock records, or prevent two
actions from spending the same balance. Keep those guarantees in application transactions.

Finish checks preserve the advisory final-message contract: network/authentication failures, invalid responses, and missing hosted proof return `verdict: "unavailable"`, `allow_finish: true`, and `outcome: "finished_unverified"`. This permits an explicitly unverified final message; it is never a passed check, authorization for a tool action, or evidence of successful execution. Applications requiring a verified finish must require `verdict === "pass"` before accepting it. Guarded tool actions still fail closed.

For applications that own execution explicitly:

```ts
const decision = await client.checks.check({
  stage: "pre",
  step: { type: "tool", name: "send_message", input: { message: "Hello" } },
});
if (!decision.allowed) throw new Error("Action blocked by Norma");
```

The client also provides agent registration/read/update, evaluator discovery, binding reads, observation delivery, and `operations.create/get/list/observe/linkVerification` for typed operation receipts. Receipts record server decisions and caller-reported observations; they do not prove that an external side effect happened. Semantic definition helpers validate and convert DTOs only. Authoring and account administration belong in the hosted Console; this package contains no authoring transport, evaluator implementation, database, or service launcher.

Public APIs expose Rules and Decisions, including agent rule lists, rule bindings, error identifiers, semantic answers, and operation receipts. The runtime and SDK use the same native Rules/Checks protocol; internal codecs only validate and map snake_case JSON to typed JavaScript fields. `public-contract.json` records the allowlisted transport surface. Changes to it and DTOs must be reviewed together; builds do not read or generate from a private server schema.

From this directory, use `make install`, then `make check`. The checks include lint, types, unit tests, a 14-file tarball allowlist, and ESM/CommonJS plus strict TypeScript consumers installed outside this repository without workspace links. Packages contain no source maps. `make release-check` prepares local artifacts; publication is disabled.

Licensed under Apache-2.0. Preserve `LICENSE` and `THIRD_PARTY_NOTICES` when redistributing.

### Public contract

Use `client.agents.listRules({ agentName })`, `client.ruleBindings.list({ ruleId })`, and `client.checks.check(...)`. A `Decision` contains `allowed`, `reason`, and `checks`; each check carries `ruleId`, `ruleName`, `checkId`, `status`, and its result. There is no public raw check escape hatch. Errors expose Rule and check identifiers only.

Operation receipts expose `rules` and a normalized `decision` (including the retained `outcome`). A historical unsupported rule remains visible with `status: "unsupported"` and a null definition; it cannot become an authorable hosted `RuleDefinition`. New operations still reject unsupported rule snapshots. Authored definitions default to hosted execution; an explicit execution value must be `"server"`.

In `4.0.0-dev.2`, `semanticDefinition()` accepts compact semantic configuration: identity and managed release may be omitted for the server to assign, and each decision rule can infer its `type` from the referenced question. Explicit release pins, identity, evidence requirements, thresholds, and uncertainty policies are preserved. Canonical runtime responses retain their recorded metadata.

### Compact semantic configuration

**Version compatibility:** `4.0.0-dev.2` supports the compact example below. The
older `4.0.0-dev.1` download requires `definition_id`, `check_release` and each
decision rule's `type` in `semanticDefinition()`. Retain those explicit fields when
using that older helper. Its control helper also requires explicit
`execution: "server"`; see the [controls guide](../docs/controls.md).

The helper validates configuration locally; it does not call a model, save a rule,
or attach it to an agent. Pass its result as the semantic evaluator's `config`
when preparing a rule in Console:

```ts
import { semanticDefinition } from "norma-sdk";

const config = semanticDefinition({
  bindings: [{ name: "message", path: "input.message" }],
  questions: [{
    id: "unsupported_promise",
    type: "statement",
    instructions: "Does the message promise an outcome without supporting evidence?",
    criteria: {
      true: "The message makes an unsupported promise.",
      false: "The message makes no unsupported promise.",
    },
  }],
  decision: {
    on_unresolved: "deny",
    rules: [{ question_id: "unsupported_promise", threshold: 0.8 }],
  },
});
```

Question `type` remains required for statement, choice and score questions;
decision rules infer their type from `question_id`. New configuration can omit
`schema_version`, `definition_id`, `definition_version`, empty `examples`, and
default binding or decision settings. Keep nonempty reference examples and all
nondefault settings, including optional bindings (`required: false`), `any`,
`at_most`, score ranges and confidence/probability thresholds. An omitted
`required_origin` means caller-supplied evidence. The current resolver cannot
supply `runtime_observed` or `source_verified`; existing stronger requirements
are retained rather than silently weakened.

A complete rule wraps the evaluator in `condition`. Semantic and finish
conditions omit `selector` because they consume the complete Step; bindings
select semantic evidence. Rego and other selected-value evaluators still require
a selector. Default Rego profile and balanced finish strictness can be omitted;
nondefault strictness remains meaningful. Authored `execution` is also optional
and defaults to `"server"`; SDK-local evaluation is unsupported. The hosted
`execution_mode`/`execution_contract` proof described above remains required on
check requests and responses.

This version uses native Rules/Checks routes and fields throughout. Semantic authoring uses statement, choice, and score forms; alternate authoring vocabularies and runtime conversion helpers are removed. Statement answers use `type: "statement"` and `support_score`. Caller-owned payloads and evidence are preserved without recursively renaming their keys.

Operation `decision.allowed` follows the validated retained operation outcome, including explicitly nonblocking observe rules and `unresolvedPolicy: "allow"`. Evaluator errors never grant new permission: historical receipts can retain `outcome: "allow"` from older permissive error policies while reporting `allowed: false`. Checkpoints remain strict and block an unsafe runtime check. A failed reconfiguration invalidates the previous client identity.

## Optional OpenAI Agents integration

Use `normaTool` from `norma-sdk/openai-agents` with separately installed `@openai/agents@0.20.0` for native function-tool guardrails and per-agent call records. See [the integration guide](https://github.com/norma-platform/norma-sdk/blob/main/typescript/OPENAI_AGENTS.md) for supported schemas and enforcement limits. Generic `checkpoint` remains available without framework dependencies.
