# VibeSys protocol contracts

`@vibesys/contracts` is the TypeScript side of the run protocol: the message
types generated from the Python models (`src/generated`, never edited by hand),
the one runtime validator that turns untrusted wire text into those types
(`parseProtocolResponse`, `parseServerMessage`, `validateRunEvent`, and the
host helper's parses), and the
error taxonomy the validator and the transports report (`BackendClientError`,
`ServerError`). It is runtime neutral (no DOM, no Node) and depends on no other
workspace package; see
[the clients architecture](../../docs/contributing/clients-architecture.md).

- `@vibesys/contracts` is the contract itself.
- `@vibesys/contracts/host-helper` is the host helper's contract: the types
  generated from `server.host_api` and the strict parse of every line a client
  reads from a helper (`parseHelperMessage`, `parseChannelEnd`), walked by the
  same validator with unknown fields, members, and message types rejected.
- `@vibesys/contracts/testing` is the test-only, runtime-neutral fixture
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

From the repository root:

```bash
pnpm --dir clients/contracts generate:protocol
pnpm --dir clients/contracts check
pnpm --dir clients/contracts test
```
