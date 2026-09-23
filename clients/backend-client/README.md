# VibeSys backend client

`@vibesys/backend-client` owns the TypeScript boundary to the Python backend
server: generated protocol types, transport-neutral parsing, request
correlation, and event subscriptions.

The package does not project events into application state and has no UI
dependency. Consumers interpret its typed messages in their own state model.

Use the package entries deliberately:

- `@vibesys/backend-client` is runtime-neutral. It exports protocol types,
  parsing, event-stream policy, and the `ServerTransport` interface.
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
