# Corpus file formats

Four file kinds live in this corpus: event fixtures under `events/`, frontend disposition tables
under `rendering-dispositions/`, connection scenarios under `scenarios/`, and runner inventories
under `runners/`. All are plain JSON so any language can read them. The corpus gate
(`clients/tools/scripts/check_conformance_corpus.mjs`) validates every file against the rules below with no
external schema library, so the rules are intentionally simple and structural. Deep payload
validation happens when the runners fold a fixture through `run-state` (888b) or replay a scenario
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
- The `RunEvent` required fields are present. The gate reads that set from the schema
  (`$defs.RunEvent.required`, currently `type` and `timestamp`) rather than hardcoding it, so the
  rule tracks the generated source.
- No top-level field outside the `RunEvent` property set (the schema declares
  `additionalProperties: false`, so an unknown field would be rejected on the wire).

Fixtures are minimal but valid: fields a kind does not need are omitted and take their schema
default. A fixture may carry the fields its kind semantically uses (for example `execution_id` on an
`agent_execution_started`), which makes it more useful to the fold runner, but the gate only requires
a structurally valid envelope. There is one fixture per kind; richer per-kind fixtures remain
separate from the rendering decisions below.

## Frontend dispositions: `rendering-dispositions/<frontend>.json`

The TUI and Web each check in one table. `events`, `queries`, `responses`, and `campaign` are exhaustive over
the generated protocol schema's `EventType` enum, `query.*` request-discriminator mappings, and
`Response` property names. `campaign` is empty until campaign contracts exist, then covers every
`Campaign*` definition and campaign-prefixed top-level schema section. Entries use the section's key
(`kind` for events and queries, `field` for responses, and `dto` for campaign contracts) and occur
exactly once. Unknown and duplicate values fail the gate.

An event is `rendered` with a non-empty `surface`, `deferred` with a positive `issue` and non-empty
`reason`, or `not_applicable` with a reason when another semantic contract deliberately supersedes
the raw event. A query is `used` with a `consumer`, `deferred`, or `not_applicable` with a reason. A
response and campaign contract may use any of those dispositions plus `rendered`. Unknown fields
and statuses are rejected, so a typo cannot satisfy coverage accidentally.

`extensions` records generated contracts planned outside the current schema. Today it contains
one `campaign-projection` deferral to #1761 and `campaign` is empty. Once the generated schema
contains a `Campaign*` definition or a campaign-prefixed top-level section, the extension set becomes
empty and those generated names must each have a disposition in `campaign`.

Deferred issue references are verified as open when a table is authored or reviewed. CI validates
the recorded positive issue number but deliberately performs no network calls, keeping the gate
deterministic and available offline.

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
- `required_setup` (array, optional): machine-readable, kebab-case capabilities that a runner must
  add before it can execute this scenario, beyond sending and receiving the declared `steps`.
  Examples are `subscription-reconnect` and `replay-failure-injection`. This field is required only
  while no runner registers the scenario, and forbidden once one does. Capability names are unique
  within the array.
- `clients` (object, optional): named actors for a simultaneous multi-client scenario. Each value
  has one `transport`, which must be listed by the scenario. Multi-client scenarios declare at
  least two actors; single-client scenarios omit this field.
- `steps` (array, non-empty): the ordered frames. Each step has:
  - `client` (string): required when the scenario declares `clients`, forbidden otherwise. It routes
    the step to that named actor while preserving one globally ordered exchange.
  - `dir`: `c2s` (client to server) or `s2c` (server to client).
  - exactly one of `frame` (the message sent) or `expect` (the message asserted on receipt). Both
    are partial protocol messages: they carry `type` and only the fields the scenario constrains. A
    runner sends `frame` verbatim and matches `expect` as a subset of the received message,
    recursively for nested objects. Arrays compare literally.
  - `type` inside `frame`/`expect` must be a known protocol message type: a `ProtocolRequest` member
    for `c2s`, or a `ServerMessage`/`Response` member for `s2c`.

The gate validates structure, decision anchoring, and whether every scenario is either registered by
a runner or declares `required_setup`. It does not execute the steps; the runners do. Keeping the
steps declarative and partial lets one scenario file drive both a Python transport replay and a
TypeScript fold without either owning the wire values.

### Host helper scenarios

A scenario with `"protocol": "host-helper"` is one channel to the host helper
(`docs/contributing/desktop-connection.md`, "The host helper") instead of a run-protocol connection.
Its `role` is `control`, `run` or `logs`, its `transports` are `["stdio"]`, and it has no `decisions` and no
`clients`. A `c2s` frame's `type` must be a helper request and an `s2c` expectation's a helper
message, both read from the generated `host-helper.schema.json`. Every helper request must be sent by
at least one helper scenario, so a new method cannot land without one. Replies are matched as subsets
like run-protocol frames; a scenario assumes an empty live registry and no project at the paths it
names, so any helper command can replay it.

## Runner inventories: `runners/<id>.json`

Each file is one runner's executable inventory. Groups correspond to distinct setup paths in that
runner, and the runner consumes these arrays as its own test parametrization rather than keeping a
second list in source.

```json
{
  "id": "server",
  "description": "Scenarios executed against the real server transports.",
  "groups": {
    "bootstrap": ["full-replay-bootstrap", "tail-bootstrap-spine-prepend"],
    "tail-overflow": ["tail-overflow-rebootstrap"]
  }
}
```

Fields:

- `id` (string): equals the filename stem.
- `description` (string): human context for what the runner exercises.
- `groups` (non-empty object): maps each kebab-case, runner-specific setup path to a non-empty array
  of scenario ids. One runner registers a scenario in exactly one group. Different runners may
  register the same scenario, since transport conformance is intentionally checked at multiple
  boundaries.

The gate rejects unknown runner fields or scenario ids, malformed groups, duplicate registration
within one runner, an unregistered scenario without `required_setup`, and a registered scenario that
still carries `required_setup`. The last rule makes removing a stale exemption part of adding runner
coverage.
