<!-- Modified for Norma. -->
# Norma SDKs

Python and TypeScript clients for the hosted Norma API. Configure the gateway URL supplied by your workspace owner and an Agent Key:

```sh
export NORMA_RUNTIME_URL="https://your-gateway.example.com/api/runtime"
export NORMA_AGENT_KEY="<your-agent-key>"
```

Pass the registered agent name explicitly to the client operation. An agent name identifies an agent inside the key's workspace; it does not select a customer or workspace.

- [Python client](python/README.md)
- [TypeScript client](typescript/README.md)
- [Workspace isolation and hostnames](docs/tenancy.md)

These packages call the hosted service. They contain no local evaluation engine, service launcher, database migration commands, deployment tools, or account administration CLI. Native host-hook installation is a separate integration, not part of these SDKs.

Use environment variables or your application's secret manager for credentials. Never commit an Agent Key. API checks can consume the workspace's configured allowance; a successful HTTP response alone does not establish that a proposed action was permitted. Read the returned check result before executing the action.

Package installation and method examples are maintained in each language guide. Repository availability does not establish registry publication or ownership of a package name.

## Optional framework integrations

Start with a Python `@norma.checkpoint(step_name="send_message")` or the TypeScript checkpoint wrapper when you own the function boundary. Framework adapters are useful when you need native blocked-call feedback or framework context; they do not automatically protect an entire agent graph.

The [Python Google ADK plugin](python/GOOGLE_ADK.md) checks selected model and tool callbacks. Install its optional extra, initialize and bind it, and place Norma first in the ADK plugin list. Earlier overriding plugins can bypass later callbacks. A denied or steered precheck prevents the selected call; a postcheck cannot undo effects.

Optional OpenAI Agents adapters attach checks to explicitly selected local function tools. The [Python Agents guide](python/OPENAI_AGENTS.md) describes explicit agent bindings and function-tool protection. The [TypeScript Agents guide](typescript/OPENAI_AGENTS.md) documents its dedicated `norma-sdk/openai-agents` entry point, supported version, setup, and exclusions. They do not intercept hosted tools, every handoff, or unwrapped functions. A steering response blocks the proposed call and offers feedback; it does not authorize a retry. Install the framework separately using the documented optional dependency.

Finish helpers check completion and are distinct from pre-tool enforcement. Streamed content may already be visible before final validation, and a final decision cannot roll back tools that ran. Every adapter still requires the correct registered identity, scoped key, and an application decision about retries and feedback.

## Hosted diagnostics

After installing the Python package, run `norma config check` to validate the two required environment variables offline. Use `norma doctor --agent NAME` to explicitly check access to a registered agent through one authenticated read. Neither command writes credentials or infrastructure. `norma version` reports the installed package version.

## Development

Run the language-specific checks described in `python/README.md` and `typescript/README.md`. Public tests use synthetic data and local HTTP test doubles; integration qualification against the hosted platform runs separately with scoped test credentials.

Before publishing an export, run `python3 tools/verify_export.py .` on the fresh source snapshot and follow [the export policy](EXPORT_POLICY.md). This verifier checks that the snapshot matches its reviewed manifest. Maintainer source, vocabulary, dependency, secret and license reviews run separately before export.

## License

The SDKs retain the included [Apache 2.0 license](LICENSE). Applicable provenance and third-party notices are retained in [Python NOTICE](python/NOTICE) and [TypeScript THIRD_PARTY_NOTICES](typescript/THIRD_PARTY_NOTICES).
