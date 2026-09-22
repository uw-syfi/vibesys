# Corpus file formats

Two file kinds live in this corpus: event fixtures under `events/` and connection scenarios under
`scenarios/`. Both are plain JSON so any language can read them. The corpus gate
(`clients/scripts/check_conformance_corpus.mjs`) validates every file against the rules below with no
external schema library, so the rules are intentionally simple and structural. Deep payload
validation happens when the runners fold a fixture through `core-state` (888b) or replay a scenario
against a live transport (#811); the gate's job is coverage and well-formedness.

## Event fixtures: `events/<kind>.json`

Each file is a single `RunEvent` instance (the model in `src/server/api/protocol.py`, generated as
`RunEvent` in `protocol.generated.ts`). The filename stem is the event's `type`.

```json
{
  "protocol_version": 1,
  "type": "run_started",
  "sequence": 1,
  "run_id": "run-conformance",
  "timestamp": "2026-01-01T00:00:00Z"
}
```

Rules the gate enforces:

- The filename stem equals `type` (`run_started.json` carries `"type": "run_started"`).
- `type` is a member of the `EventType` enum in the generated schema.
- Required fields are present: `type` and `timestamp`.
- No top-level field outside the `RunEvent` property set (the schema declares
  `additionalProperties: false`, so an unknown field would be rejected on the wire).

Fixtures are minimal but valid: fields a kind does not need are omitted and take their schema
default. A fixture may carry the fields its kind semantically uses (for example `execution_id` on an
`agent_execution_started`), which makes it more useful to the fold runner, but the gate only requires
a structurally valid envelope. There is one fixture per kind; richer per-kind fixtures and the
rendering-disposition tables are #834, not here.

## Scenarios: `scenarios/<id>.json`

Each file is one connection-level exchange: a role, the framing decisions it exercises, and the
ordered sequence of frames in each direction.

```json
{
  "id": "full-replay-bootstrap",
  "title": "Bootstrap a fresh subscription with a full replay",
  "description": "A subscribe from sequence 0 with no tail is acknowledged, then one bootstrap batch carries the whole history with history_after_sequence 0.",
  "role": "subscribe",
  "transports": ["unix", "websocket"],
  "decisions": ["WP-GRANULARITY", "WP-NEWLINE", "WP-ROLES", "WP-SUBSCRIBE-ACK"],
  "steps": [
    {"dir": "c2s", "frame": {"type": "subscribe", "after_sequence": 0}},
    {"dir": "s2c", "expect": {"type": "subscribed"}},
    {"dir": "s2c", "expect": {"type": "event_batch", "history_after_sequence": 0}}
  ]
}
```

Fields:

- `id` (string): equals the filename stem. Stable; runners key on it.
- `title`, `description` (string): human context. `description` should state the observable behavior,
  because that is what a runner asserts.
- `role` (string): one of `control`, `subscribe`, `chat` (see `WP-ROLES`). Which connection kind the
  scenario runs on.
- `transports` (array): the transports that must reproduce it, a subset of `["unix", "websocket"]`.
  A scenario that exercises a stream-only framing concern may be `["unix"]`; most are both.
- `decisions` (array): the wire-contract decision tokens this scenario exercises. Every token must
  appear as a decision in `docs/contributing/wire-protocol.md`. Across the whole corpus, every token
  in the contract must be tagged by at least one scenario.
- `steps` (array, non-empty): the ordered frames. Each step has:
  - `dir`: `c2s` (client to server) or `s2c` (server to client).
  - exactly one of `frame` (the message sent) or `expect` (the message asserted on receipt). Both
    are partial protocol messages: they carry `type` and only the fields the scenario constrains. A
    runner sends `frame` verbatim and matches `expect` as a subset of the received message.
  - `type` inside `frame`/`expect` must be a known protocol message type: a `ProtocolRequest` member
    for `c2s`, or a `ServerMessage`/`Response` member for `s2c`.

The gate validates structure and the decision anchoring. It does not execute the steps; the runners
do. Keeping the steps declarative and partial lets one scenario file drive both a Python transport
replay and a TypeScript fold without either owning the wire values.
