# VibeSys backend client

`@vibesys/backend-client` owns the TypeScript boundary to the Python backend
server: the protobuf wire types (generated into `src/gen` from
`proto/server/wire/v2` by `pnpm proto:generate`), the JSON codec, JSONL framing,
request correlation, and event subscriptions. Test builders live in
`@vibesys/backend-client/testing`.

The package does not project events into application state and has no UI
dependency. Consumers interpret its typed messages in their own state model.

From the repository root:

```bash
pnpm --dir clients/backend-client check
pnpm --dir clients/backend-client test
pnpm --dir clients/backend-client build
```
