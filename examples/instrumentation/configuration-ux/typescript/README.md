# Clearer selectors and connection names

This current sample uses the TypeScript v2 declarative API with one `configure()` call for two existing identities. The JSON primary config and YAML peer config show the `input` selector, `application` aliases for the existing platform fields, and the user-facing `rule-controls` spelling. Their stems remain the exact agent names.

Requires TypeScript SDK `4.0.0-dev.11` or later.

The peer YAML retains the `typescript-local-omlx-trial` application label, `configuration-parity` version, and request fields used by the earlier sample. This is a new current sample with a deterministic stream; the historical native-tool trial config and reports remain unchanged.

Save [`app.ts`](app.ts) in your application, install the TypeScript SDK build, set `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` in the server environment, then run the file with your TypeScript runner. Create or select both identities and attach any Rules in Console first. Startup reads those identities and enabled Rules; it does not create identities or attach Rules. The source tests load these same files with a public `HTTPClient` transport double and make no model or hosted API calls.

`parameter-names.example.yaml` is a separate optional snippet. Do not place it inside `norma-agents/`: it demonstrates explicit TypeScript positional names and is not loaded by the two-agent runner.

The ordinary application functions take one request object, so their selectors use `[input, field]`. Native OpenAI tools also use `input` for their decoded JSON object. `params` is reserved for a declaration that includes `parameter_names`; TypeScript maps those names to actual positional argument indexes without inspecting source text or function length.

The application label is evidence recorded on the turn. It is not a Console Application identity and it does not grant authorization. The friendly `rule-controls` config value normalizes to the existing `rule-controls-v1` transport token; the runtime must still return both exact acknowledgement fields.
