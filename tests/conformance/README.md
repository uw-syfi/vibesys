# Transport conformance corpus

One shared, language-neutral fixture corpus that the transport conformance suites read.
The browser port (#808) runs two transports carrying the same protocol: the existing Unix domain
socket and a WebSocket gateway (#811). Both must reproduce the framing and connection semantics
written down in [`docs/contributing/wire-protocol.md`](../../docs/contributing/wire-protocol.md).
This corpus is the fixtures that hold them to it.

There is exactly one copy. The server-side scenario, relay and host helper suites read the files
here and keep no private copy, so a fixture change moves all of them at once. The TypeScript
client suites that once read it were deleted with the legacy client libraries.

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
  protocol schema (`server.api.schema.protocol_json_schema`). `test_corpus_files.py` enumerates the
  enum and fails if a kind has no fixture, so a new event kind cannot land without one. Each file is a single valid `RunEvent` instance; the filename stem is its `type`.

  These fixtures are not serialization goldens. Message shape is owned by the generated schema (and,
  as protocol codegen matures, by the codegen source explored in #850): the tests read the envelope
  and its required fields from that schema rather than re-encoding them here, so the fixtures do not
  duplicate what codegen owns. Their job is the two things codegen does not provide: a coverage
  checklist that forces every event kind to exist, and one representative instance per kind for the
  fold and scenario runners to replay.
- `scenarios/` holds the connection-level exchanges (bootstrap, resume, rebootstrap, capability
  probes, protocol error, and so on). Each scenario tags the wire-contract decisions it exercises.
- `runners/` holds the executable inventory each runner consumes as its own parametrization. A
  scenario no runner registers must declare the machine-readable setup capability it still needs.

## What reads this

| Reader | Language | Lands in | Uses |
| --- | --- | --- | --- |
| server scenario suite | Python (`tests/conformance/test_server_transports.py`) | follow-up to #811 | replays the bootstrap, control-path, and simultaneous two-client scenarios on every transport each declares |
| relay runner | Python (`tests/e2e/test_relay.py`) | #1785 | replays the Unix bootstrap, control-path, and tail-overflow scenarios through a real host helper's run channel |
| host helper runner | Python (`tests/conformance/test_host_helper.py`, `tests/e2e/test_host_helper.py`) | #1887 | replays the `helper-*` scenarios against the helper in process and over stdio against a real helper command (`VIBESYS_HOST_HELPER_COMMAND` points it at another implementation) |
| subscription frame corpus | Go emits and pins it (`gateway/internal/subscription/corpus_test.go`) | #3498 | what the real hub sends for scripted producers, in `fixtures/subscription/frames.json` |

The server scenario, relay and host helper suites exist today. Do not treat structural validation
of a fixture as evidence that a scenario has executed against a transport.

### What executes today

The checked-in inventories are `runners/server.json`, consumed directly by the Python server scenario
suite, `runners/relay.json`, consumed by the relay runner, and `runners/host-helper.json`,
consumed by the host helper runner. All three replay steps through
`replay.py`. The remaining scenarios carry `required_setup` in their own files.

A control-path reply is a `Response`, which is deliberately outside the `type`-discriminated
`ServerMessage` union, so scenarios name it with the pseudo-type `response`. The Python runner
derives that name in `frame_matching.py` from the section the protocol schema publishes `Response`
under, and checks the claim by validating the received frame as a `Response`, so a discriminated
server message can never satisfy it.

## Running the suites

From the repository root:

```bash
uv run pytest -q tests/conformance                      # server scenarios, host helper and frame matching
```

See [`FORMAT.md`](FORMAT.md) for the exact file shapes.
