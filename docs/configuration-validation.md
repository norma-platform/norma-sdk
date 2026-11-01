# Validate configuration offline

Run the installed SDK validator before starting the application. Validation checks configuration files without importing application code or contacting Norma.

```sh
norma config validate ./norma-agents/support.yaml
norma config validate ./norma-agents --json
```

The command accepts one `.json`, `.yaml`, or `.yml` file, or scans only immediate supported files in a directory in filename order. In a TypeScript project, the package provides the `config:validate` script; use the installed local binary:

```sh
npm run --silent config:validate -- ./norma-agents/support.yaml
npm run --silent config:validate -- ./norma-agents --json
```

If your application package does not expose a script, add:

```json
{
  "scripts": {
    "config:validate": "norma config validate"
  }
}
```

## Validate a Check input contract

Add `input_schema` to a version 2 checkpoint when Rules depend on required Check input fields. The SDK validates the schema offline and checks a literal `input_from.value` during configuration. For dynamic input, it validates the final Check input before sending a precheck or running the action. Invalid input stops before the Check request; diagnostics identify the config pointer and JSON instance path without echoing values.

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    input_from: { value: { subject: "Balance" } }
    input_schema:
      type: object
      required: [recipient]
      properties:
        recipient: { type: string }
```

Use the same configuration as JSON:

```json
{
  "version": 2,
  "entrypoints": [{ "target": "application.run" }],
  "checkpoints": {
    "send_message": {
      "target": "tools.send_message",
      "input_from": { "value": { "subject": "Balance" } },
      "input_schema": {
        "type": "object",
        "required": ["recipient"],
        "properties": { "recipient": { "type": "string" } }
      }
    }
  }
}
```

Run `norma config validate ./norma-agents/support-agent.yaml` (or use the installed TypeScript CLI). This literal is missing `recipient`, so validation reports a schema error at `/checkpoints/send_message/input_from/value` with invalid instance path `/recipient`. Add the required field or change the contract. An input schema guards Check shape; it does not prove Rule attachment or predicate behavior.

An omitted `$schema` uses Draft 7. The only accepted explicit URI is `http://json-schema.org/draft-07/schema#`; references must be local JSON Pointers. The SDK rejects `$id`, `format`, unknown keywords, external or unresolved references, and cycles. It does not coerce values, insert defaults, or remove properties.

## Check Rule path coverage

Run the offline check with the local configuration and Rule declarations you want to inspect. A Rule file applies to every selected configuration; with a Rule directory, each configuration uses the Rule file with the same filename stem.

```sh
norma config check ./norma-agents/support-agent.yaml --rules ./rules/support-agent.json
norma config check ./norma-agents --rules ./rules --json
```

The check reads only local files. It does not import the application, read credentials or request values, call providers, evaluate conditions, or contact Norma. It follows selector paths through values known from the configuration. A required object path in checkpoint `input_schema` can establish structural presence for dynamic Check input; optional fields or nullable fields required to be non-null remain unknown. A known literal input takes precedence. Dynamic values and unsupported expressions without a structural guarantee are reported as unknown. For a JSON evaluator, `required_fields` entries and keys in `field_types` or `field_constraints` add presence paths; their values and predicates are not evaluated. Dotted keys traverse object properties. Wildcard or expression syntax stays unknown. Python `input_from: all_kwargs` has known object shape but unknown keys; TypeScript `input_from: all_params` names possible keys but cannot prove which positional slots were supplied. Selectors for those fields remain unknown offline.

Unknown coverage fails by default. `--allow-unknown` waives only unknown coverage; syntax, schema, missing paths, and I/O errors still fail. The report includes `rules_checked`, `coverage_complete`, and one coverage item per declared path and configured checkpoint stage. Each item is `covered`, `missing`, or `unknown`. `coverage_complete` stays false when any path is missing or unknown, even when unknowns are allowed. Coverage does not predict whether the next call passes `input_schema`, whether a Rule is attached, or whether its predicate matches.

### Example: fix a missing Rule path

This configuration supplies only `input.to`, while the Rule declares `input.from`:

```yaml
version: 2
entrypoints:
  - target: application.run
checkpoints:
  send_message:
    target: tools.send_message
    input_from: { value: { to: "person@example.test" } }
```

```json
{
  "name": "recipient-source-check",
  "condition": {
    "selector": { "path": "input.from" },
    "evaluator": { "name": "json", "config": {} }
  }
}
```

Save these as `norma-agents/support-agent.yaml` and `rules/support-agent.json`, then run:

```sh
norma config check ./norma-agents/support-agent.yaml --rules ./rules/support-agent.json --json
```

The checker marks `input.from` as `missing` at the checkpoint's pre and post stages, with diagnostic code `coverage_missing`. Change the selector to `input.to` to match the configured Check input.

This path check is not a full Rule validator. Opaque evaluators such as Rego, SQL, custom evaluators, and `finish_claims` remain unknown; regex scope is not executed. Python and JavaScript regex engines can differ; for portable scope patterns, prefer explicit ASCII classes such as `[0-9]` and `[A-Za-z]`, and avoid engine-specific constructs. It proves declared-path presence from configuration only, not invocation values, predicate results, Rule attachment, or data freshness. See the [configuration reference](configuration-reference.md#rule-path-coverage) for accepted Rule inputs and report fields.

## What validation checks

| Check | Offline validator | Application startup |
| --- | --- | --- |
| File syntax | JSON/YAML syntax, duplicate keys, supported extensions | Loads the full selected file set |
| Schema | Required fields, value shapes, strict unknown-field rejection | Runtime option validation |
| Capability | Settings supported by the selected SDK | Bound object and native adapter resolution |
| Selector | Path shape, phase restrictions, declared TypeScript parameter names | Python callable signatures and actual invocation values |
| Identity | File stem and in-memory identity shape | Authenticated reads of existing agent identities and enabled Rules |
| Application code | Not imported or executed | Targets and callbacks resolved from bindings |
| Network | No credentials read and no requests sent | Credentials, target authorization, and runtime access checked |

The validator does not resolve application bindings, invoke providers, read request values, execute callbacks, or contact the runtime. Startup verifies actual targets, callbacks, authorization, and network access. TypeScript can validate declared positional names offline. Python derives formal parameter names from loaded callables at startup.

## Use diagnostic output in CI

Exit codes are `0` for valid files, `1` for invalid files or read failures, and `2` for command-line usage errors. With `--json`, stdout contains a structured report:

```json
{
  "valid": false,
  "language": "typescript",
  "files_checked": 1,
  "diagnostics": [{
    "file": "/work/norma-agents/support.yaml",
    "code": "schema",
    "path": "/entrypoints/0/turn/finish/on_hold",
    "message": "Required field is missing.",
    "line": 8,
    "column": 7
  }]
}
```

`path` is a JSON Pointer. Locations are one-based when available. Diagnostic codes include `syntax`, `schema`, `capability`, `selector`, `identity`, and `io`. Messages omit rejected configuration values.

## Associate the schema in your editor

Download the shared JSON Schema file into your project, such as `schemas/instrumentation.schema.json`, then associate it with JSON and YAML files in VS Code:

[Download instrumentation.schema.json](../schema/instrumentation.schema.json)

```json
{
  "json.schemas": [{
    "fileMatch": ["norma-agents/*.json"],
    "url": "./schemas/instrumentation.schema.json"
  }],
  "yaml.schemas": {
    "./schemas/instrumentation.schema.json": "norma-agents/*.{yaml,yml}"
  }
}
```

Editor schema support provides structural checks and completion. The SDK validator additionally checks language-specific capabilities that the shared schema cannot express. Do not add a `$schema` property to a configuration file; unknown fields are rejected.

See the [configuration reference](configuration-reference.md) for supported fields and the [configuration guide](configuration-guides.md) for runnable examples.
