# VibeSys transport

`@vibesys/transport` owns the runtime-neutral TypeScript transport to the Python
backend server: framing, the control channel with request correlation, redial
and backoff, the persistent event stream, the stream reconciler, and request
policy. The protocol's types, its runtime validator, and its error taxonomy live
in [`@vibesys/contracts`](../contracts/README.md). No TypeScript client dials a
run's Unix control socket: the desktop app reaches a host through the Python host
helper, which does (`vs_project.api.OperatorDialer`).

The package does not project events into application state and has no UI
dependency. Consumers interpret its typed messages in their own state model.

Its entries, each compiled with only its runtime's library and types
(`docs/contributing/clients-architecture.md`):

- `@vibesys/transport` (`neutral`) exports the event-stream policy,
  `StreamReconciler`, `NewlineFramer`, the `ControlChannel` an adapter drives,
  and the `ServerTransport` interface. `StreamReconciler` decides how each
  `event_batch` and history backfill folds against what a subscription already
  delivered (store identity, the history floor, the replayed spine, and
  supersession across a re-bootstrap). It returns batch dispositions and folds
  nothing itself. For history, it proposes a floor that the consumer accepts
  only after its state model accepts the prefix. The fetch is injected per
  call, so every client shares the arithmetic without this package learning
  any state model or reaching a transport. The timer and UUID globals it uses
  are declared in `src/host-globals.d.ts`, so a Node or DOM API is a type error.
- `@vibesys/transport/websocket` (`dom`) is the browser WebSocket transport. It
  uses one WebSocket per protocol role and treats one text frame as one
  protocol message.
- `@vibesys/transport/testing` (`neutral`) exports `FakeClock`, the one
  deterministic timer Fake every client package's tests share.

From the repository root:

```bash
pnpm --dir clients/transport check
pnpm --dir clients/transport test
pnpm --dir clients/transport build
```
