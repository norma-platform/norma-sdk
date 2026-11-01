# Streamed finish

This small example streams two string chunks from an existing async generator and
checks their combined text only after natural exhaustion. It uses the standard
configured Agent scope, creates fresh IDs, and does not install an `on_verdict`
callback.

Copy `config/example-agent.json` to `config/<exact-agent-name>.json`, where the file
stem is the name of an existing Agent. Set `NORMA_AGENT_NAME` to that same name, and
set `NORMA_RUNTIME_URL` and `NORMA_AGENT_KEY` to the runtime URL and scoped key issued
for it. Then run `python app.py`. Startup reads that Agent and its enabled Rules; it
does not create an Agent or attach Rules. The sample emits canned local text and does
not call an AI model.

The chunks are visible to the consumer before the finish check runs. A hold raises
`FinishHeldError` after the final chunk and exposes the accumulated text as
`error.original_result`. An unavailable finish check follows the SDK's existing
fail-open behavior; normal exhaustion does not prove that a Rule passed.
