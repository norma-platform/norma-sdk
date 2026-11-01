# Streamed finish checks

Use a generic stream finish when an entrypoint returns an async iterator. Norma checks the selected text after natural iterator exhaustion. The application function is not rerun and the stream is not replayed.

## Configure stream selection

The same configuration can be written in YAML or JSON. With no text selector, the whole yielded value must be a string. Use a ValueSource path to select a string from a structured chunk.

```yaml
version: 2
entrypoints:
  - target: application.stream
    turn:
      session_id: { path: [input, session_id] }
      turn_id: { path: [input, turn_id] }
      finish:
        stream:
          text: { path: [result, text] }
          mode: concat
          max_chars: 8000
          missing: error
        on_hold: raise
checkpoints: {}
```

```json
{
  "version": 2,
  "entrypoints": [{
    "target": "application.stream",
    "turn": {
      "session_id": { "path": ["input", "session_id"] },
      "turn_id": { "path": ["input", "turn_id"] },
      "finish": {
        "stream": {
          "text": { "path": ["result", "text"] },
          "mode": "concat",
          "max_chars": 8000,
          "missing": "error"
        },
        "on_hold": "raise"
      }
    }
  }],
  "checkpoints": {}
}
```

`result` selects the current yielded chunk while resolving `finish.stream.text`. `mode` accepts `concat` (default) or `last`. `max_chars` accepts 1 through 8000 Unicode code points and defaults to 8000. Overflow raises `CheckError`, closes the source iterator, and sends no partial finish. `missing` accepts `error` (default) or `skip`; skip applies only to a missing path or a selected null. A callback failure, accessor path, or non-string selection is an error. Empty natural exhaustion checks an empty string.

## Stream lifecycle

Each value and the iterator's natural terminal return value are forwarded unchanged. Norma retains only the bounded selected text. The finish verdict arrives after chunks reach the consumer, so it governs completion and cannot retract emitted content.

The check runs when iteration naturally completes. Early return, cancellation, consumer break, unhandled throw, iterator error, and close do not finish the turn. A handled throw may yield and continue; the finish check still runs at most once.

Python supports synchronous and asynchronous generator entrypoints. TypeScript supports native async generators. For a regular function, declare `returns: "async_iterable"` for an `AsyncIterable` factory or `returns: "promise"` for a `Promise<AsyncIterable>` factory. A declared factory is called on the first iterator advance, not at configuration startup. Checkpoint generators are unsupported. Keep the configured handle open until the stream is consumed.

The target, context, and turn selectors resolve when iteration starts. Configured agent context is active during iterator advance, throw/return handling, and finalization. Advances are serialized; concurrent calls share one iterator and produce at most one finish request. Closing a configured handle prevents further configured work while allowing iterator cleanup.

## Handle a held finish

With `on_hold: raise`, `FinishHeldError` carries the verdict and the aggregate text: Python exposes `original_result`; TypeScript exposes `result`. With `on_hold: return`, iteration ends normally and the configuration must also provide `on_verdict`. The SDK does not automatically retry or continue streamed output.

See the [configuration guide](configuration-guides.md#configure-streamed-finish-checks) and [configuration reference](configuration-reference.md#turns-and-finish) for all finish settings.
