# Streamed finish for a TypeScript async generator

This example configures one existing agent and one async-generator entrypoint. It uses a single `configure()` call, fresh session and turn IDs, and `on_hold: raise` without a custom verdict callback.

The YAML file `norma-agents/streaming-agent.yaml` maps its file stem to the existing `streaming-agent` identity. It selects the session and turn IDs from the generator arguments and concatenates the yielded string chunks for the finish check.

Save [`app.ts`](app.ts) as `app.ts` in your application. Install the [qualified SDK tarball](https://norma-platform.cloud/artifacts/norma-sdk-4.0.0-dev.15.tgz) using the TypeScript SDK setup guide, set `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` in the server environment, then run the file with your TypeScript runner. Create or select the `streaming-agent` identity and attach any finish Rules in Console first.

The example prints each delta as it arrives and checks the concatenated text only after natural exhaustion. It creates new IDs for each invocation and closes the configured handle when the process exits.
