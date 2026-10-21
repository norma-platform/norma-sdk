<!-- Modified for Norma. -->
# Moving to the hosted clients

The hosted clients use an explicit gateway URL and an Agent Key. They do not install or start the platform.

1. Obtain the gateway URL and a scoped Agent Key from your workspace owner.
2. Set `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY`. Keep the key in environment configuration or a secret manager.
3. Register the agent through the workspace's supported setup flow, then pass its exact name explicitly in SDK calls. The key must authorize that agent.
4. Use the hosted check result to decide whether the proposed action may proceed. Instrumented actions stop on authentication failures, unavailable or malformed responses, and missing hosted-protocol confirmation. A failed postcheck cannot undo an action that already ran.
5. Verify allowed, denied, malformed-input, and unavailable-service cases in your integration before replacing a live path.

Local evaluator configuration, service startup, direct database access, and selectable server authentication modes are not supported by these hosted clients. Rule and policy administration stays in the platform console. Existing applications using those features need an explicit migration; dropping their imports is not equivalent behavior.

## Required runtime contract

For checks, finish checks, and new retained operations, the hosted clients send `execution_mode: "hosted"` and require the check response to confirm `execution_contract: "hosted-v1"`. The service must reject unsupported local rules using the same effective-rule snapshot that it evaluates. A separate client lookup cannot provide that guarantee if the rule configuration changes before evaluation.

An older runtime that ignores the request mode and omits confirmation is incompatible: the SDK must stop the instrumented action instead of accepting a partial check. This is a release gate, not an optional client setting. The compatible runtime must be deployed and the account gateway configured and compatibility-tested before these packages replace an existing installation. Local qualification is not a production rollout. Historical operation receipts remain readable without inventing hosted confirmation for them.

Finish checks retain a distinct completion contract: an unavailable or unconfirmed result is explicitly unverified, never a successful rule assessment. They do not execute a tool or undo effects. Read each language guide for completion handling.

The Python package provides `norma config check` for offline validation, `norma version` for the installed package version, and `norma doctor --agent NAME` for one authenticated, read-only agent access check. Diagnostics never print the key or URL and never save credentials. Doctor does not evaluate a rule, prove tenant-host binding, or verify the hosted check contract. There are no server, database, deployment, login, or key-creation commands. Host-hook installers remain separate.

See the [Python](../python/README.md) and [TypeScript](../typescript/README.md) guides for the exact supported API. See [tenancy](tenancy.md) for workspace and hostname boundaries.

## Public application models

Use Rules, Checks and Decisions throughout application code. A Decision contains `allowed`, an optional `reason`, and `checks`. Each check identifies its Rule, its check observation, and its outcome. Python uses snake_case field names and TypeScript uses camelCase. Persisted operation receipts and framework callbacks follow the same vocabulary. The HTTP protocol uses native Rule and Check routes and identifiers. SDK adapters normalize wire snake_case into the documented language conventions.

Public Rule definitions describe hosted execution only. Runtime configuration received from the service is validated separately so that an unsupported local Rule is rejected, never silently discarded. A checkpoint called before successful initialization raises without invoking the decorated function.

## Protocol visibility

The HTTP contract and application models share the native Rule and Check vocabulary. Public packages contain no compatibility layer for retired routes or identifiers. This release requires the coordinated native runtime and gateway migration; older clients and runtimes are incompatible. Python's private wire modules are technically importable but unsupported. TypeScript exposes only its declared package entry points.

Python `NormaAPIError` retains its underlying `httpx` request and response for diagnostics. TypeScript's `HTTPClient` hooks also expose requests and responses. These transport surfaces may contain upstream identifiers and sensitive payloads; use normalized error diagnostics for application logging. Caller-owned metadata and evidence retain their original keys. Legal attribution remains in the included notices.

## Optional framework adapters

A generic checkpoint remains appropriate when you own the callable. In Python, put `@norma.checkpoint(step_name="send_message")` on the function before initialization. With OpenAI Agents, place `@function_tool(failure_error_function=None)` above it when a failed check must stop the run instead of becoming an ordinary tool-error message. Do not combine a generic checkpoint and native adapter on the same boundary unless duplicate checks are intentional.

Native OpenAI Agents integration is an optional convenience for explicitly selected local function tools, including framework feedback and attribution. It does not automatically cover hosted tools, handoff callbacks, nested agents' internal tools, or direct calls to an unwrapped function. Consult the [Python Agents guide](../python/OPENAI_AGENTS.md) or [TypeScript Agents guide](../typescript/OPENAI_AGENTS.md) before attaching a shared tool to multiple framework agents: a display name is not authorization to use a different registered Norma identity. Denial, unavailable service, and malformed checks must block invocation; steering must skip the current invocation before returning feedback. Independent concurrent calls are not an atomic transaction.

For [Google ADK](../python/GOOGLE_ADK.md), bind the plugin after successful initialization and put it first in the plugin list. Keep all prechecks enabled unless reduced coverage is deliberate. Model steering now blocks the current generation and returns guidance rather than modifying instructions and continuing unchecked. A revised proposal requires a new check. Postchecks suppress results after execution; they do not undo or replay side effects.

Framework dependencies remain optional. These integrations cover their documented lifecycle callbacks, not all possible execution paths. Completion helpers and tracing are not substitutes for a pre-effect check, and streaming consumers must not assume final validation can retract previously emitted content.
