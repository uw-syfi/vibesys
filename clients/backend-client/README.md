# VibeSys backend client

`@vibesys/backend-client` owns the TypeScript boundary to the Python backend
server: generated protocol types, JSONL framing, request correlation, and event
subscriptions.

The package does not project events into application state and has no UI
dependency. Consumers interpret its typed messages in their own state model.

## Browser transport

Import `BrowserBackendClient`, `PersistentEventStream`, `ServerError`, and
protocol types from `@vibesys/backend-client/browser`. This export has no
Node runtime dependencies. The default client base URL is
`window.location.origin`; an explicit HTTP(S) base URL is also accepted.

```ts
import {BrowserBackendClient, PersistentEventStream} from '@vibesys/backend-client/browser';

const client = new BrowserBackendClient();
const stream = new PersistentEventStream(client, {tail: 300});
const snapshot = await client.request({type: 'query.snapshot'});
```

Requests use `POST /api/request`. Independent WebSocket subscriptions use
`/api/events` with `ws:` or `wss:` matching the base URL. Both transports use
the generated protocol envelopes. Readiness, pending command acknowledgments,
replay floors, and checkpoints pass through unchanged. Consumers own state
projection and must distinguish acknowledgment from lifecycle completion.

Call `stream.close()` and `client.close()` on teardown. Closing a browser
client aborts its requests and subscriptions, without stopping the backend.
Ordinary requests time out after 30 seconds and subscription handshakes after
5 seconds (configurable through the second constructor argument). Chat has
no client response deadline. Protocol errors retain structured diagnostics.

See [gateway startup](../../src/server/README.md) and
[browser workspace](../web/README.md).

From the repository root:

```bash
pnpm --dir clients/backend-client generate:protocol
pnpm --dir clients/backend-client check
pnpm --dir clients/backend-client test
pnpm --dir clients/backend-client build
```
