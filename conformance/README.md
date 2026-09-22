# Transport conformance corpus

One shared, language-neutral fixture corpus that both frontend transport conformance suites read.
The browser port (#808) runs two transports carrying the same protocol: the existing Unix domain
socket and a WebSocket gateway (#811). Both must reproduce the framing and connection semantics
written down in [`docs/contributing/wire-protocol.md`](../docs/contributing/wire-protocol.md). This
corpus is the fixtures that hold them to it.

There is exactly one copy. The client-side fold suite (`@vibesys/backend-client`, #812) and the
server-side scenario suite (`src/server`, #811) both read the files here. Neither keeps a private
copy, so a fixture change moves both suites at once.

## Layout

```
conformance/
  README.md                 this file
  FORMAT.md                 the fixture and scenario file formats
  events/<kind>.json        one RunEvent fixture per EventType enum member
  scenarios/<id>.json       one connection scenario per file
```

- `events/` proves event-kind coverage. There is one file per member of the `EventType` enum in the
  generated schema (`clients/backend-client/src/generated/protocol.schema.json`). The corpus gate
  enumerates the enum and fails if a kind has no fixture, so a new event kind cannot land without
  one. Each file is a single valid `RunEvent` instance; the filename stem is its `type`.
- `scenarios/` holds the connection-level exchanges (bootstrap, resume, rebootstrap, capability
  probes, protocol error, and so on). Each scenario tags the wire-contract decisions it exercises,
  so every decision in the contract is covered by at least one scenario and no scenario references a
  decision that does not exist.

## What reads this

| Reader | Language | Lands in | Uses |
| --- | --- | --- | --- |
| corpus gate | Node (`clients/scripts/check_conformance_corpus.mjs`) | 888a (this) | validates coverage, structure, and decision anchoring |
| client fold runner | TypeScript | 888b | replays scenarios through `core-state`, asserts identical folds |
| server scenario suite | Python | #811 | replays scenarios against a live transport, both Unix and WebSocket |

Only the gate exists today. It is the CI enforcement that keeps the corpus well formed and complete
while the runners are built on top of it.

## Running the gate

From the TypeScript workspace root:

```bash
cd clients
node scripts/check_conformance_corpus.mjs      # one-shot check, prints violations and exits non-zero
pnpm test:conformance                          # the same check under node --test, with failure-mode tests
```

`pnpm test:clients` runs `test:conformance` as part of the client test suite, so CI enforces it.

The gate checks, with no external dependency:

1. Every `EventType` enum member has an `events/<kind>.json` fixture, and no fixture names a kind
   that is not in the enum.
2. Every event fixture is a structurally valid `RunEvent`: its `type` matches its filename and the
   enum, its required fields are present, and it carries no unknown top-level fields.
3. Every scenario is structurally valid: its `id` matches its filename, its steps are well formed,
   and every frame it names is a known protocol message type.
4. Every wire-contract decision token (`WP-*` in `wire-protocol.md`) is exercised by at least one
   scenario, and every decision a scenario tags exists in the contract. This is what keeps the
   document and the corpus from drifting apart.

See [`FORMAT.md`](FORMAT.md) for the exact file shapes.
