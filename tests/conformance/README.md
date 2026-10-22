# Transport conformance corpus

One shared, language-neutral fixture corpus that both frontend transport conformance suites read.
The browser port (#808) runs two transports carrying the same protocol: the existing Unix domain
socket and a WebSocket gateway (#811). Both must reproduce the framing and connection semantics
written down in [`docs/contributing/wire-protocol.md`](../../docs/contributing/wire-protocol.md).
This corpus is the fixtures that hold them to it.

There is exactly one copy. The client-side fold suite (`@vibesys/transport`, #812) and the
server-side scenario suite (`src/server`, #811) both read the files here. Neither keeps a private
copy, so a fixture change moves both suites at once.

## Layout

```
tests/conformance/
  README.md                 this file
  FORMAT.md                 the fixture and scenario file formats
  events/<kind>.json        one RunEvent fixture per EventType enum member
  rendering-dispositions/   one generated-contract disposition table per frontend
  scenarios/<id>.json       one connection scenario per file
  runners/<id>.json         runner-owned executable scenario inventory
```

- `events/` proves event-kind coverage. There is one file per member of the `EventType` enum in the
  generated schema (`clients/libs/contracts/src/generated/protocol.schema.json`). The corpus gate
  enumerates the enum and fails if a kind has no fixture, so a new event kind cannot land without
  one. Each file is a single valid `RunEvent` instance; the filename stem is its `type`.

  These fixtures are not serialization goldens. Message shape is owned by the generated schema (and,
  as protocol codegen matures, by the codegen source explored in #850): the gate reads the envelope
  and its required fields from that schema rather than re-encoding them here, so the fixtures do not
  duplicate what codegen owns. Their job is the two things codegen does not provide: a coverage
  checklist that forces every event kind to exist, and one representative instance per kind for the
  fold and scenario runners to replay.
- `rendering-dispositions/` records what the TUI and Web frontend do with every generated event,
  query, response field, and generated campaign DTO/section. Each member is rendered or used, explicitly inapplicable, or deferred
  with an open owning issue. The Web table covers both browser and Electron renderers because they
  compose the same feature packages. A whole-contract campaign-projection deferral points to #1761
  until that backend-owned generated DTO exists; the gate then requires one disposition for every
  `Campaign*` definition and campaign-prefixed top-level section and rejects the old extension deferral.
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
| corpus gate | Node (`clients/tools/scripts/check_conformance_corpus.mjs`) | 888a (this) | validates coverage, structure, decision anchoring, and the execution partition |
| event fold runner | TypeScript (`clients/libs/run-state/src/conformance.test.ts`) | follow-up to 888a | folds every shared event fixture through `run-state`, both batched and incrementally |
| client scenario runner | TypeScript | 888b | replays connection scenarios through the client transport and `run-state` |
| server scenario suite | Python (`tests/conformance/test_server_transports.py`) | follow-up to #811 | replays the bootstrap, control-path, and simultaneous two-client scenarios on every transport each declares |
| relay runner | Python (`tests/e2e/test_relay.py`) | #1785 | replays the Unix bootstrap, control-path, and tail-overflow scenarios through a real host helper's run channel |
| host helper runner | Python (`tests/conformance/test_host_helper.py`, `tests/e2e/test_host_helper.py`) | #1887 | replays the `helper-*` scenarios against the helper in process and over stdio against a real helper command (`VIBESYS_HOST_HELPER_COMMAND` points it at another implementation) |

The corpus gate, event fold runner, and server scenario suite exist today. The connection-level
client runner is still outstanding. Do not treat structural validation or event folding as evidence
that a scenario has executed against a transport.

### What executes today

The checked-in inventories are `runners/server.json`, consumed directly by the Python server scenario
suite, `runners/relay.json`, consumed by the relay runner, and `runners/host-helper.json`,
consumed by the host helper runner. All three replay steps through
`replay.py`. The remaining scenarios carry `required_setup` in their own files. This section deliberately
does not repeat either list: `node clients/tools/scripts/check_conformance_corpus.mjs` derives the complete
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
node clients/tools/scripts/check_conformance_corpus.mjs       # one-shot check, prints violations and exits non-zero
pnpm --dir clients test:conformance                     # the same check under node --test, with failure-mode tests
pnpm --dir clients --filter @vibesys/run-state test    # folds the shared event fixtures through core state
uv run pytest -q tests/conformance/test_server_transports.py  # exercises both server transports
```

`pnpm test:clients` runs `test:conformance` as part of the client test suite, so CI enforces it.

The gate checks, with no external dependency:

1. Every `EventType` enum member has an `events/<kind>.json` fixture, and no fixture names a kind
   that is not in the enum.
2. Every event fixture is a structurally valid `RunEvent`: its `type` matches its filename and the
   enum, its required fields are present, and it carries no unknown top-level fields.
3. The TUI and Web tables classify every schema-derived `EventType`, `query.*` request,
   `Response` field, and campaign DTO/section exactly once. A new generated member therefore fails with an actionable missing
   disposition in both frontends. Deferred entries carry a positive owning issue and a reason;
   issue liveness is verified when the table is reviewed rather than by a network call in CI.
4. Every scenario is structurally valid: its `id` matches its filename, its steps are well formed,
   and every frame it names is a known protocol message type.
5. Every wire-contract decision token (`WP-*` in `wire-protocol.md`) is exercised by at least one
   scenario, and every decision a scenario tags exists in the contract. This is what keeps the
   document and the corpus from drifting apart.
6. Every scenario is registered by at least one runner or declares the setup capability still
   required, never both. Runner inventories may name only checked-in scenarios and cannot register
   one scenario twice within the same runner.

See [`FORMAT.md`](FORMAT.md) for the exact file shapes.
