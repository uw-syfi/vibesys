# VibeSys backend client

`@vibesys/backend-client` owns the TypeScript boundary to the Python backend
server: generated protocol types, transport-neutral parsing, request
correlation, and event subscriptions.

The package does not project events into application state and has no UI
dependency. Consumers interpret its typed messages in their own state model.

Use the package entries deliberately:

- `@vibesys/backend-client` is runtime-neutral. It exports protocol types,
  parsing, event-stream policy, `StreamReconciler`, and the `ServerTransport`
  interface. `StreamReconciler` decides how each `event_batch` and history
  backfill folds against what a subscription already delivered (store
  identity, the history floor, the replayed spine, and supersession across a
  re-bootstrap). It returns batch dispositions and folds nothing itself. For
  history, it proposes a floor that the consumer accepts only after its state
  model accepts the prefix. The fetch is injected per call, so both clients
  share the arithmetic without this package learning either state model or
  reaching a transport.
- `@vibesys/backend-client/testing` is the test-only, runtime-neutral fixture
  surface shared by client packages. Production imports are rejected by the
  architecture gate. Its defaults are deliberately small and deterministic:
  `timestamp(n)` is `n` seconds after `2026-01-01T00:00:00Z`; `event` adds no
  run, round, or agent identity; `roundFinishedEvent` is completed round 1 with
  one passing attempt, no performance measurement, and `profile_skipped:
  false`; `statusEvent` is an implementer event in
  `round-1-implementer`, with matching execution and invocation ids;
  `chatEvent` adds only chat scope, never status, thread, or invocation
  identity; `snapshotResponse` is running `run-1` at
  sequence zero; and `eventBatch` adds no cursor metadata. Scenario-specific
  evidence and identity must be supplied explicitly. Every call owns its
  returned objects and container arrays.
- `@vibesys/backend-client/node` is the TUI's Unix-domain-socket transport.
- `@vibesys/backend-client/websocket` is the browser WebSocket transport. It
  uses one WebSocket per protocol role and never imports a Node builtin.

The Node and browser adapters share the parser and transport interfaces. Their
different framing rules are local to the adapters: Unix uses `NewlineFramer`,
while WebSocket treats one text frame as one protocol message.

From the repository root:

```bash
pnpm --dir clients/backend-client generate:protocol
pnpm --dir clients/backend-client check
pnpm --dir clients/backend-client test
pnpm --dir clients/backend-client build
```
