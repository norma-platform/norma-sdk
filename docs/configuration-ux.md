# Select values from application calls

ValueSource paths select existing values without evaluating code. Use `input` for the first request value and `params` when you need a named parameter. These roots supplement `args`, `kwargs`, `result`, `error`, and `context`.

| Root | Selects | Runtime rule |
| --- | --- | --- |
| `input` | First user input | Python uses the first signature-bound user parameter, including keyword/default binding and excluding a bound-method receiver. TypeScript uses the first supplied positional argument. A native OpenAI tool uses its decoded JSON input. |
| `params` | A named formal parameter | Python maps the bound callable signature, including keyword-only parameters. TypeScript maps positional names declared by `parameter_names` on the same target. |

Example for a TypeScript function with positional request and options parameters:

```yaml
entrypoints:
  - target: application.handle
    parameter_names: [request, options]
    turn:
      session_id: { path: [params, request, session_id] }
      turn_id: { path: [input, turn_id] }
```

TypeScript names must be nonblank and unique. It maps declared names to actual positional indices and does not inspect function source or length, apply target defaults during selection, or probe a function at startup. If an invocation omits a slot, its `params` selection is missing. Extra and rest arguments remain available through `args`.

Python maps `params` from the callable signature and rejects TypeScript-only `parameter_names`. Native OpenAI tool declarations use `input` and reject `params` and `parameter_names`.

Paths use safe traversal. They do not invoke accessors or evaluate expressions. A missing selected path fails closed. `result` and `error` are available only in fields that run after those values exist.

## Turn metadata names

Version 2 turn configuration accepts `application` and `application_version` as aliases for `platform` and `platform_version`. Supply either spelling; if both appear, values must match. The SDK emits the existing `platform` and `platform_version` turn fields. When omitted, the existing application label is `python-sdk` or `typescript-sdk`, and no application version is added. These values are turn metadata and do not select a Console Application or grant access.

## Control contract names

Configuration accepts `rule-controls` or `rule-controls-v1`. Both normalize to the existing `rule-controls-v1` wire token and keep the existing request header and response acknowledgement contract.

See the [configuration reference](configuration-reference.md#value-sources) for every field, default, and language-specific limit. Use [offline validation](configuration-validation.md) to check selectors and runtime capability without importing application code.
