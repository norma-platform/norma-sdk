<!-- Modified for Norma. -->
# Norma SDKs

Install the supplied Python wheel or TypeScript package, configure a JSON/YAML file, validate it, and bind one existing agent to application functions. Start with the [configuration guide](docs/declarative-instrumentation.md), then choose a language guide.

## Start with configuration

1. [Install the Python SDK](https://norma-platform.cloud/artifacts/norma_sdk-8.7.1.dev18-py3-none-any.whl) or [TypeScript SDK](https://norma-platform.cloud/artifacts/norma-sdk-4.0.0-dev.15.tgz) from the supplied archive. Use the exact downloaded filename in your install command.
2. Read [configuration guides](docs/configuration-ux.md) for YAML-first examples and selector behavior.
3. Run [offline configuration validation](docs/configuration-validation.md) before startup.
4. Review the [configuration reference](docs/declarative-instrumentation.md) for supported fields and limits.

Python setup and APIs are in [python/README.md](python/README.md). TypeScript setup and APIs are in [typescript/README.md](typescript/README.md). The same declarative schema is supported by both SDKs, with documented language-specific limits. Configuration validates existing identities and Rules; it does not create agents or attach Rules.

These packages call the hosted service. They contain no local evaluation engine, runtime service, database migrations, deployment tools, or account administration CLI. Create and attach Rules in Console. The hosted runtime performs execution; the SDK returns decisions to the application, which controls whether and how an action runs.

## Advanced SDK

Direct clients, manual checkpoint decorators and wrappers, explicit checks, and framework adapter details remain available in the language guides. Use those APIs when configuration does not cover the integration you need. An adapter protects only its documented call boundaries; it does not automatically cover an entire agent graph. A precheck can stop a selected action; a postcheck cannot undo an effect that already happened.

Use environment variables or a secret manager for credentials. Never commit an Agent Key. Read the returned decision before executing a proposal.

## Development

Run the language-specific checks described in `python/README.md` and `typescript/README.md`. Public tests use synthetic data and local HTTP test doubles; hosted integration checks are separate.

Before preparing an export, run `python3 tools/verify_export.py .` and follow [the export policy](EXPORT_POLICY.md). The SDKs retain the included [Apache 2.0 license](LICENSE). Applicable notices are in [Python NOTICE](python/NOTICE) and [TypeScript THIRD_PARTY_NOTICES](typescript/THIRD_PARTY_NOTICES).
