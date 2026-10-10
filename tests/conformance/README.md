# Transport conformance corpus

One shared, language-neutral fixture corpus that both frontend transport conformance suites read.
The browser port (#808) runs two transports carrying the same protocol: the existing Unix domain
socket and a WebSocket gateway (#811). Both must reproduce the framing and connection semantics
written down in [`docs/contributing/wire-protocol.md`](../../docs/contributing/wire-protocol.md).
This corpus is the fixtures that hold them to it.

There is exactly one copy. The client-side fold suite (`@vibesys/backend-client`, #812) and the
server-side scenario suite (`src/server`, #811) both read the files here. Neither keeps a private
copy, so a fixture change moves both suites at once.

## Layout

```
tests/conformance/
  README.md                 this file
  FORMAT.md                 the fixture and scenario file formats
  events/<kind>.json        one RunEvent fixture per EventType enum member
  scenarios/<id>.json       one connection scenario per file
  runners/<id>.json         runner-owned executable scenario inventory
```

- `events/` proves event-kind coverage. There is one file per member of the `EventType` enum in the
  generated schema (`clients/backend-client/src/generated/protocol.schema.json`). The corpus gate
  enumerates the enum and fails if a kind has no fixture, so a new event kind cannot land without
  one. Each file is a single valid `RunEvent` instance; the filename stem is its `type`.

  These fixtures are not serialization goldens. Message shape is owned by the generated schema (and,
  as protocol codegen matures, by the codegen source explored in #850): the gate reads the envelope
  and its required fields from that schema rather than re-encoding them here, so the fixtures do not
  duplicate what codegen owns. Their job is the two things codegen does not provide: a coverage
  checklist that forces every event kind to exist, and one representative instance per kind for the
  fold and scenario runners to replay.
- `scenarios/` holds the connection-level exchanges (bootstrap, resume, rebootstrap, capability
  probes, protocol error, and so on). Each scenario tags the wire-contract decisions it exercises,
  so every decision in the contract is covered by at least one scenario and no scenario references a
  decision that does not exist.
- `runners/` holds the executable inventory each runner consumes as its own parametrization. The
  corpus gate derives execution coverage from these files. A scenario no runner registers must
  declare the machine-readable setup capability it still needs, and that declaration becomes an
  error as soon as a runner registers it.

## What reads this

| Reader | Language | Lands in | Uses |
| --- | --- | --- | --- |
| corpus gate | Node (`clients/scripts/check_conformance_corpus.mjs`) | 888a (this) | validates coverage, structure, decision anchoring, and the execution partition |
| event fold runner | TypeScript (`clients/core-state/src/conformance.test.ts`) | follow-up to 888a | folds every shared event fixture through `core-state`, both batched and incrementally |
| client scenario runner | TypeScript | 888b | replays connection scenarios through the client transport and `core-state` |
| server scenario suite | Python (`tests/conformance/test_server_transports.py`) | follow-up to #811 | replays the bootstrap, control-path, and simultaneous two-client scenarios on every transport each declares |
| stdio bridge runner | Python (`tests/e2e/test_stdio_bridge.py`) | #1785 | replays the Unix bootstrap, control-path, and tail-overflow scenarios through a real stdio bridge process |

The corpus gate, event fold runner, and server scenario suite exist today. The connection-level
client runner is still outstanding. Do not treat structural validation or event folding as evidence
that a scenario has executed against a transport.

### What executes today

The checked-in inventories are `runners/server.json`, consumed directly by the Python server scenario
suite, and `runners/stdio-bridge.json`, consumed by the stdio bridge runner. Both replay steps through
`replay.py`. The remaining scenarios carry `required_setup` in their own files. This section deliberately
does not repeat either list: `node clients/scripts/check_conformance_corpus.mjs` derives the complete
partition and fails when a scenario is in neither half or both.

A control-path reply is a `Response`, which is deliberately outside the `type`-discriminated
`ServerMessage` union, so scenarios name it with the pseudo-type `response`. The Python runner
derives that name in `frame_matching.py` from the section the generated schema publishes `Response`
under, and checks the claim by validating the received frame as a `Response`, so a discriminated
server message can never satisfy it. The gate restates the literal because it is a separate
process; `test_frame_matching.py` fails if the two disagree.

## Running the gate

From the repository root:

```bash
node clients/scripts/check_conformance_corpus.mjs       # one-shot check, prints violations and exits non-zero
pnpm --dir clients test:conformance                     # the same check under node --test, with failure-mode tests
pnpm --dir clients --filter @vibesys/core-state test    # folds the shared event fixtures through core state
uv run pytest -q tests/conformance/test_server_transports.py  # exercises both server transports
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
5. Every scenario is registered by at least one runner or declares the setup capability still
   required, never both. Runner inventories may name only checked-in scenarios and cannot register
   one scenario twice within the same runner.

See [`FORMAT.md`](FORMAT.md) for the exact file shapes.
