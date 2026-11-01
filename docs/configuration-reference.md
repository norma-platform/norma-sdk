# Configuration reference

This page describes the supported version 2 JSON/YAML document. Every configuration file belongs to one existing agent identity; the lowercase filename stem selects that identity. version, entrypoints, and checkpoints are required. Unknown keys are rejected. Fields that a language SDK cannot consume are rejected before remote identity reads.

Start with the [configuration guides](configuration-guides.md) for runnable examples or use [offline validation](configuration-validation.md) before application startup.

## Root fields

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| version | Integer 2 | Required | Selects version 2 format. |
| entrypoints | Nonempty array of unique target paths or entrypoint objects | Required | Binds application entrypoints to this agent. |
| checkpoints | Mapping of step name to checkpoint definition | Required; empty mapping allowed | Binds function checkpoints. |
| connection | Connection object | Optional | Shared connection configuration. |
| telemetry | Telemetry object | Optional; Python supports documented fields; TypeScript supports enabled only | Configures observability. |
| refresh | { interval_seconds: integer } | Optional; Python defaults to 60 seconds; zero disables refresh. TypeScript rejects this key. | Sets enabled-Rule refresh interval. |
| schemas | Schema settings object | Optional; publish defaults to false | Supplies expected Steps and opts into compatible Step publication. |
| operations | Mapping of operation name to operation definition | Optional; defaults to none | Installs retained operation wrappers. |
| adapters | Object mapping names to adapter definitions | Optional; defaults to empty object | Connects documented native framework hooks. |

## Connection settings

File connection values can be overridden by shared and per-agent configure() options. SDK environment defaults are used when values are omitted.

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| runtime_url | Nonblank string or { env: NAME } | Optional; NORMA_RUNTIME_URL fallback | Runtime base URL. |
| agent_key | { env: NAME } or { binding: path } | Optional; NORMA_AGENT_KEY fallback | Agent credential. |
| transport | { binding: path } | Optional; Python only | Python transport. TypeScript rejects it. |
| http_client | { binding: path } | Optional; TypeScript only | TypeScript HTTP client. Python rejects it. |
| debug_logger | { binding: path } | Optional; TypeScript Norma logger only | Python rejects this field. |
| user_agent | Nonblank string | Optional; SDK client default; TypeScript only | TypeScript user-agent string. |
| timeout_ms | Integer >= 1 | Optional; Python defaults to 30000 ms; TypeScript has no general timeout when omitted | Request timeout in milliseconds in both SDKs. |
| target_type | Nonblank string | Optional; paired with target_id | Explicit request target type. Standard Agent Keys use configured agent scope. |
| target_id | Nonblank string | Optional; paired with target_type | Explicit request target ID. |
| control_contract | rule-controls or rule-controls-v1 | Optional; omitted means no control contract is explicitly selected | Both values use the rule-controls-v1 wire token. |

## Schema publication

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| schemas.publish | Boolean | Optional; false | Publish compatible Step schemas for an existing authorized agent. |
| schemas.steps | Array of schema entries | Optional; no Steps declared | Lists expected schemas. |
| schemas.steps[].type | Nonblank string | Required per entry | Step type. |
| schemas.steps[].name | Nonblank string | Required per entry | Step name. |
| schemas.steps[].description | String or null | Optional | Step description. |
| schemas.steps[].input_schema | Object or null | Optional | Input JSON schema; existing input contracts cannot change. |
| schemas.steps[].output_schema | Object or null | Optional | Output JSON schema; existing output contracts cannot change. |
| schemas.steps[].metadata | Object or null | Optional | Step metadata; omitted values remain unchanged. |

Publication updates compatible Steps on an existing agent. It does not create identities or change Rules, evaluators, agent metadata, or unmentioned Steps. Each agent update is atomic; a directory is not one remote transaction.

## Entrypoints, context, target, and trace

An entrypoint can be a dotted target string or an object.

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| target | Dotted binding path | Required in object form | Function or framework boundary to wrap. |
| parameter_names | Array of unique nonblank strings | Optional; TypeScript only | Declares positional aliases for `params` selectors; the SDK does not inspect the function signature to verify those names. Python rejects this field. |
| returns | promise or async_iterable | Optional; TypeScript return declaration; async_iterable for stream finish | Declares the return shape without startup probing. |
| context | ValueSource | Optional; empty object | Selects request context. |
| target_scope.type | ValueSource | Required with target_scope.id | Selects target type. |
| target_scope.id | ValueSource | Required with target_scope.type | Selects target ID. |
| trace.trace_id | ValueSource | Required with trace.span_id | Selects nonzero W3C trace ID. |
| trace.span_id | ValueSource | Required with trace.trace_id | Selects nonzero W3C span ID. |
| turn | Turn object | Optional | Adds turn identity and finish behavior. |

For standard Console Agent Keys, omit target_scope or select the configured agent identity. Configuration does not grant authorization.

## Value sources

A ValueSource has exactly one form: { value: JSON }, { path: [...] }, or { provider: binding.path }. A bound callback reference { binding: path } is separate and accepted only where executable code or a native object is expected.

| Path root | Meaning | Runtime limits |
| --- | --- | --- |
| args | Python positional arguments; TypeScript positional array | Both runtimes. |
| kwargs | Python keyword arguments | TypeScript supplies empty kwargs. |
| result | Application result | Only after a result exists. |
| error | Application error | Only after an error exists. |
| context | Configured request context | Both runtimes. |
| input | First user input | Python first bound user argument, excluding the method receiver; TypeScript first supplied positional argument; OpenAI tools use decoded input. |
| params | Named formal/positional parameter | Python bound signature including keyword-only values, excluding the method receiver; TypeScript requires `parameter_names` on the same target and selects supplied slots. |

Paths are nonempty arrays of string object keys and nonnegative integer array indices. Selectors use safe traversal, do not evaluate expressions or invoke accessors, and fail when a selected path is missing. Python excludes the method receiver regardless of its parameter name. Ordinary function parameters named `self` or `cls` remain inputs. TypeScript `parameter_names` assigns aliases to positional slots; the SDK neither infers names/defaults from the target nor verifies that aliases match the function's source. An omitted positional slot remains missing even when JavaScript applies a default.

ValueSource providers receive invocation fields args, kwargs, result, error, context, and agent_name. In Python, kwargs contains only values explicitly supplied by keyword; it does not include positional values or omitted defaults. TypeScript supplies an empty kwargs object. These callbacks resolve through bindings and are not executed during offline validation. Checkpoint state_provider uses the separate flat configure() providers registry and receives the checkpoint target arguments; see the [provider setup guide](configuration-guides.md#register-a-checkpoint-state-provider).

## Turns and finish

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| session_id | ValueSource | Required when turn exists | Selects session ID. |
| turn_id | ValueSource | Required when turn exists | Selects turn ID. |
| user_request | ValueSource | Optional | Selects request text. |
| platform / application | String | Optional; python-sdk or typescript-sdk | Application label; aliases must match if both supplied. |
| platform_version / application_version | String | Optional; absent from metadata if omitted | Application version; aliases must match if both supplied. |
| model | String | Optional; absent if omitted | Model metadata. |
| max_continuations | Integer 0–3 | Optional; 2 | Bounds finish continuation count. |
| finish.driver | generic, openai_agents, pydantic_ai | Generic for generic form; native driver requires adapter; native drivers Python only | Selects finish integration. |
| finish.final_message | ValueSource | Required for generic non-stream finish | Selects completed response text. |
| finish.stream.text | ValueSource | Optional; selects yielded value itself when omitted | Selects text from each yielded item. |
| finish.stream.mode | concat or last | Optional; concat | Joins selected chunks or uses last selected text. |
| finish.stream.max_chars | Integer 1–8000 | Optional; 8000 | Maximum collected text. |
| finish.stream.missing | error or skip | Optional; error | Behavior for missing selected text. |
| finish.on_hold | raise or return | Required for generic finish | Raises with verdict or returns result unchanged; return requires on_verdict. |
| finish.on_verdict | Bound callback | Optional; required with on_hold return | Receives invocation fields, result, turn, and verdict. |
| finish.continue_with | Bound callback | Optional; generic non-stream only | Produces a replacement result checked under the same turn recorder. |
| finish.adapter | Nonblank registry name | Required with native finish driver | Selects configured native finish adapter. |

Python supports synchronous and asynchronous generator streams. TypeScript supports async generators and declared AsyncIterable or Promise<AsyncIterable> factories. Chunks are forwarded before the finish decision; only natural exhaustion triggers the check. The entrypoint is not replayed.

## Checkpoints and payload mapping

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| target | Dotted binding path | Required | Function called for this named checkpoint. |
| parameter_names | Unique nonblank string array; empty allowed | Optional; TypeScript only; required with checkpoint `input_from: all_params` | Declares names in positional call order for `params` selectors and `all_params`. |
| step_type | Nonblank string | Optional; tool | Step type sent to checks. |
| state | JSON object (65,536 UTF-8 bytes, depth 32, 10,000 nodes maximum) | Optional; mutually exclusive with state_provider and state_from | Static state snapshot. |
| state_provider | Nonblank exact string key in providers registry | Optional; mutually exclusive with state and state_from | Python calls the provider with checkpoint target arguments bound with defaults; TypeScript passes supplied positional arguments. See the [provider setup guide](configuration-guides.md#register-a-checkpoint-state-provider). |
| state_from | ValueSource | Optional; mutually exclusive with state and state_provider | Selects state once and reuses the snapshot for postcheck. |
| action.kind | shell, edit, read, search, web, mcp, task, other | Required when action appears | Declares action kind. |
| action.target / action.command / action.cwd | String, null, or ValueSource | Optional; cwd is checkpoint-only | Declares action evidence. Native tool and operation records reject cwd. |
| input_schema | Draft 7 JSON Schema object | Optional; version 2 checkpoints only | Validates the final Check input before a precheck or action. Literal input_from.value is checked during configuration validation. |
| input_from / output_from | ValueSource; Python checkpoint `input_from` also accepts `all_kwargs`; TypeScript checkpoint `input_from` also accepts `all_params` | Optional | Maps application data or a result directly into Check input/output. `all_kwargs` copies explicitly supplied Python keyword arguments. `all_params` builds an object from supplied TypeScript positional slots named by the checkpoint's `parameter_names`; missing/default slots and extra unnamed arguments are omitted. Each shorthand is rejected by the other SDK. Without a mapping, a TypeScript tool Check input wraps positional values in `args`; an LLM Check input uses the raw argument array. See the [keyword argument mapping guide](configuration-guides.md#map-python-keyword-arguments-into-one-check-input). |
| context_from | ValueSource | Optional; defaults to an empty object | Selects the Check context. The resolved value must be a JSON object; a path returns the selected value unchanged and does not wrap scalars. Norma-owned context fields cannot be replaced. |
| tools_from / ground_truth_from | ValueSource | Optional | Maps tool and ground-truth facts into the Check payload. |
| options | Registry name or inline object | Optional | Selects supported native checkpoint options. |

`input_schema` uses Draft 7; omitting `$schema` selects Draft 7, and the only accepted explicit URI is `http://json-schema.org/draft-07/schema#`. References must be local JSON Pointers. `$id`, `format`, unknown keywords, external or unresolved references, and cyclic references are rejected. Validation does not coerce values, insert defaults, or remove properties. TypeScript bounds an inlined schema to 4,096 JSON value nodes and depth 100 before compilation; repeated acyclic references count toward the limit. This resource guard is TypeScript-specific; Python does not inline the schema. See [input validation](configuration-validation.md#validate-a-check-input-contract) and [Rule path coverage](configuration-validation.md#check-rule-path-coverage) for offline behavior.

Python inline options support state, action, action_kind, action_target_arg, action_command_arg, and action_cwd_arg. TypeScript supports client, stepName, stepType, targetType, targetId, state, and action. Python rejects client, stepName, stepType, targetType, and targetId; TypeScript rejects Python action_*_arg keys. Native OpenAI tool and retained operation evidence reject cwd because records do not retain it.

| Inline options key | Type and accepted values | Requirement / default | Runtime support |
| --- | --- | --- | --- |
| client | Bound NormaClient reference | Optional; TypeScript only; defaults to the configured client | If supplied, must be the client configured for this agent. Python rejects it. |
| stepName | Nonblank string | Optional; TypeScript only; defaults to checkpoint mapping key | If supplied, must match checkpoint name. Python rejects it. |
| stepType | Nonblank string | Optional; TypeScript only; defaults to checkpoint step_type or tool | If supplied, must match checkpoint type. Python rejects it. |
| targetType / targetId | Nonblank strings | Optional; TypeScript only; must be supplied together; configured client target applies when omitted | Selects checkpoint target. Python rejects both fields. |
| action | Action object or bound callback reference | Optional; conflicts with checkpoint action | Python binding resolves to an action mapping; TypeScript binding resolves to an action callback. |
| state | JSON object or bound callback reference | Optional; conflicts with declarative state fields | Python binding may resolve to a static state value or callback; TypeScript binding must resolve to a callback. |
| action_kind | shell, edit, read, search, web, mcp, task, other | Optional; Python only | Maps action kind from function arguments. |
| action_target_arg / action_command_arg / action_cwd_arg | Nonempty strings | Optional; Python only | Name the arguments used for action target, command, and working directory. |

`bindings` is required in Python and TypeScript configure() options; pass `{}` when the document has no bound references. `providers` is an optional flat registry keyed by exact strings for checkpoint `state_provider`; dots in a key are literal. This differs from a ValueSource `{ provider: "payload.build" }`, which resolves a dotted path through `bindings` and receives invocation data. `options` is an optional registry of native checkpoint options referenced by name. The `connections` option is an exact-agent-name mapping and is not a file-level root field.

## Rule path coverage

`norma config check <config> --rules <rules>` checks declared Rule selector paths against the selected local configuration. A rules file is shared across selected configurations. A rules directory matches files to configurations by filename stem; every selected configuration needs one match, and duplicate stems fail. Other files in the rules directory are ignored. JSON and YAML rule inputs can be a definition, a list, a `rule` or `rules` wrapper, or a Console draft with `jsonText`. The draft `jsonText` supplies the Rule definition and scope; `agentName` limits identity only, and other draft fields are not merged. An empty Rule list is valid; a nonempty draft `agentName` must match the selected agent. The check is offline and structural; it does not evaluate predicates, invoke application code, or verify Rule attachment.

By default, unknown paths fail. `--allow-unknown` waives only unknown coverage; missing paths and validation errors still fail. The JSON report adds `rules_checked`, `coverage_complete`, and deterministic `coverage` items with the configuration file, Rule file and index, declaration path, checkpoint, stage, selector, and status. `coverage_complete` is false if any path is missing or unknown, regardless of the override. This is path analysis, not full Rule validation: opaque Rego, SQL, custom, and `finish_claims` dependencies remain unknown, and regex scope is not executed. Python and JavaScript regex engines can differ; use explicit ASCII classes such as `[0-9]` and `[A-Za-z]` for portable scope patterns, and avoid engine-specific constructs.

See [offline validation](configuration-validation.md#check-rule-path-coverage) for the command, rule-file selection, and limits.

## Retained operations

| Key | Type and accepted values | Required / omitted behavior | Purpose |
| --- | --- | --- | --- |
| target | Dotted binding path | Required | Application function to wrap. |
| operation_key | ValueSource | Required | Stable operation proposal key. |
| parameter_names | Unique nonblank strings | Optional; TypeScript only | Names positional parameters for selectors; Python rejects it. |
| returns | promise | Optional; declaration for non-native async TypeScript target | Declares async return. |
| input / context / evidence / tools | ValueSource | Optional | Selects proposal fields. |
| action.kind | Shared action enum | Required if action supplied | Declares action kind. |
| action.target / action.command | String or ValueSource | Optional; cwd rejected | Declares action target or command. |
| unresolved_policy | block or allow | Optional; block | Chooses unresolved decision behavior. |
| observations.attempted / succeeded / failed | Observation object | Optional | Selects caller-reported observations. |
| Observation event_key / summary | ValueSource | Required per observation | Identifies and describes event. |
| Observation external_reference | ValueSource | Optional | Selects external reference. |
| verification_session_id | ValueSource | Optional | Links an existing verification session. |
| on_record | Bound callback | Optional | Receives operation records. |

Each observation requires event_key and summary; external_reference is optional. The succeeded phase cannot select an error, and the failed phase cannot select a result because those values are not available in those phases. Observations are attributed application evidence, not independent proof of an external effect. Operation keys deduplicate proposals; application code owns business idempotency.

## Framework adapters

| kind | Required | Optional | Runtime |
| --- | --- | --- | --- |
| openai_agents | kind=openai_agents and agent bound reference | runner bound reference (Python only); tools mapping | Python and TypeScript. Python supports native tools; TypeScript definition-form tools. |
| google_adk | kind=google_adk and runner bound reference | agent; enabled_hooks (all four when omitted or empty); blocked_message_template (omitted uses raw reason; {reason} placeholder); step_name_overrides (empty); step_name_resolver; context_extractor; on_violation_callback; enable_logging (true) | Python. Hooks: before_model, after_model, before_tool, after_tool. |
| strands | kind=strands and agent bound reference | event_types (all seven when omitted or empty); on_violation_callback; enable_logging (true) | Python. Events: BeforeInvocationEvent, BeforeModelCallEvent, AfterModelCallEvent, BeforeToolCallEvent, AfterToolCallEvent, BeforeNodeCallEvent, AfterNodeCallEvent. |
| pydantic_ai | kind=pydantic_ai and agent bound reference | No additional fields | Python. |

`adapters` is a named mapping, not a profile list. Per-agent connection presets are supplied through the `connections` configure API option. OpenAI adapter tools may be a native-tool reference or a definition-form object. Definition form requires description, strict JSON object parameters with additionalProperties=false, and bound execute; optional fields are needs_approval (false), input_guardrails and output_guardrails (empty arrays), on_record (bound callback), and action (kind, optional target and command). A Python native-tool entry requires bound tool and accepts check_output (false), context_extractor, and action. TypeScript rejects Python native FunctionTool entries.

## Telemetry and refresh

The shared schema describes Python telemetry settings. TypeScript accepts telemetry.enabled only and rejects other telemetry fields.

| Key | Type and accepted values | Omitted behavior | Runtime and purpose |
| --- | --- | --- | --- |
| enabled | Boolean | true | Python and TypeScript; enables event delivery. |
| sink_name | Nonblank string | default | Python built-in names: default, registered, otel; or a registered custom factory. |
| sink_config | JSON object | {} | Python; configuration passed to selected sink. |
| batch_size | Integer >= 1 | 100 | Python; events per batch. |
| flush_interval | Number > 0 | 5.0 seconds | Python; automatic flush interval. |
| max_retries | Integer >= 0 | 3 retries | Python; retries after failed delivery. |
| retry_delay | Number > 0 | 1.0 second | Python; retry delay. |
| shutdown_join_timeout | Number > 0 | 5.0 seconds | Python; worker shutdown wait. |
| shutdown_flush_timeout | Number > 0 | 5.0 seconds | Python; shutdown flush wait. |
| shutdown_max_failed_flushes | Integer >= 1 | 1 | Python; failed shutdown flushes before stopping. |
| otel_enabled | Boolean | false | Python; enables OpenTelemetry event sink. |
| otel_endpoint | Nonblank string or environment reference | null | Python; OTLP HTTP endpoint. |
| otel_headers | Object of strings or environment references | {} | Python; OTLP request headers. |
| otel_service_name | Nonblank string | norma-sdk | Python; OpenTelemetry service name. |
| otel_tracer_provider | Bound reference | Optional; no provider supplied | Python; existing tracer provider. |
| logging.config | Bound reference resolving to a dictionary | Optional; no process logging config applied | Python logging.config.dictConfig settings. |
| logging.log_enabled | Boolean | true | Python logging master switch. |
| logging.log_span_start | Boolean | true | Python logs span start. |
| logging.log_span_end | Boolean | true | Python logs span end. |
| logging.log_rule_eval | Boolean | true | Python logs Rule evaluations. |
| logging.log_span_results | Boolean | true; accepted but no effect | Python accepted no-op. |
| refresh.interval_seconds | Integer >= 0 | Python defaults to 60 seconds; zero disables periodic refresh | Python only; enabled-Rule refresh interval. |

Close the configured handle during shutdown. Python aclose() and TypeScript close() restore owned bindings, flush observability, and return delivery receipts. A Python custom sink factory is executable setup and owns its batching and retries.

## Configure API options

configure() requires bindings and accepts optional providers, options, agent_name (Python) or agentName (TypeScript) for in-memory configurations, and connection options. connections is a configure option keyed by exact agent name, not a file-level root field. Shared and per-agent options override file values. File and directory inputs derive identity from the exact filename stem and reject an identity override.
