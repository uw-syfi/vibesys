# Transport wire contract

This document is the single owner of the framing and connection semantics that every transport
carrying the frontend protocol must reproduce. It exists because the browser port (#808) adds a
second transport, a WebSocket gateway (#811), beside the existing Unix domain socket
(`src/server/transport/unix_jsonl.py`), and the two are built by different people in different
languages. Anything a transport can get wrong independently is written down here so the two ends
cannot silently disagree.

## Scope

This contract is transport level, not payload level. It specifies how protocol messages are framed,
which connection carries which role, how errors and disconnects are expressed, and how liveness and
flow control behave. It does not specify the schema of the messages themselves: those are the
Pydantic models in `src/server/api/protocol.py` (built on the light control envelope in
`src/server/control_envelope.py`), generated into
`clients/libs/contracts/src/generated/`. A transport relays whole protocol messages opaquely. If the
payload schema changes (for example a version bump), this layer does not.

One payload invariant is relevant to framing: a recorded `RunEvent` is bounded by
`src/server/events.py:MAX_SERIALIZED_RUN_EVENT_BYTES` before it reaches either transport.
`src/server/events.py:EventStore.append`
keeps an ordinary event byte-for-byte unchanged. If an event is larger, it retains prefixes of its
variable-size payload values on Unicode code-point and collection-entry boundaries and carries
`truncated: true`; clients render that marker. The durable log holds the bounded event,
so live delivery and every later replay observe the same fact. The WebSocket batch chunker still
handles only whole events and has no payload-specific branch.

It also does not specify optional HTTP asset serving by the WebSocket gateway. That behavior has no
counterpart on the Unix transport and is outside the `WP-*` protocol decisions here.

The authoritative message taxonomy is three unions in `src/server/api/protocol.py`:

| Direction | Union | Members |
| --- | --- | --- |
| client to server | `ProtocolRequest` | `command.*`, `query.*`, `subscribe` (18 types) |
| server to client, control | `Response` | one per request, correlated by `request_id` |
| server to client, stream | `ServerMessage` | `subscribed`, `event`, `event_batch`, `active_executions_chunk`, `protocol_error` |

`PROTOCOL_VERSION` is `1`. Every model sets `extra="forbid"`, which is load bearing: a client probes
for an optional field by sending it and treating rejection as "this server does not have the field"
(see `WP-CAPABILITY-PROBE`).

`query.iterations` is an additive control request in that generated payload
schema. A response returns one bounded page of roots or direct children, or a
typed unavailable result. Its opaque cursor binds projection revision, parent,
and offset; clients must replay an identical page idempotently and reject a
different page at an already applied cursor. Transports do not decode the cursor
or interpret iteration kinds. Older servers reject the request under the same
strict request parsing, and clients retain event-derived round navigation as the
compatibility path.

## Measurement comparison events

`measurement_compared` carries one comparison of a candidate with its base: `metric`, `unit`,
`direction`, the candidate and base commits, their mean values over `candidate_samples` and
`base_samples` runs, the `ratio` (candidate over base), the `noise` (one standard error of the
ratio) and a `verdict` of `better`, `within_noise`, `worse` or `inconclusive` (no estimate of the noise yet). Core logs a comparison whenever a
new sample changes it; the run's commit observer emits one event per logged comparison, in order.
Folding is by order, and a later event for the same candidate supersedes an earlier one.

## Run trace events

This is payload level, recorded here because every client folds it the same way. The run trace
(see "The run trace" in [flows.md](flows.md)) reaches clients as three generic run events,
`span_started`, `span_updated` and `span_ended`. Each event carries the **whole span** in
`data.span`, plus the event's own `sequence` and `timestamp`, never a field delta. The runtime
records the spans. After each durable core commit, the run's commit observer
(`vibesys.run.integration`) diffs the committed trace against the one before
(`vs_core.span_changes`) and emits one event per span that started, changed or ended, in the
trace's order. A span that starts and ends within one commit gets a `span_started` and then a
`span_ended`. No search code is involved.

- **Folding.** Replace by `span.id`, in `sequence` order. A span keeps the position it first
  started at, so each span comes after its parent. `RunSnapshot.spans` is that fold at the
  snapshot's `sequence`. The server folds the same events as a journal listener
  (`src/server/trace.py`), live and on replay. A client starts from the snapshot and folds the events
  after it (`@vibesys/run-state/trace`: `traceFromSnapshot`, `foldTrace`). Folding every span event
  gives the snapshot's spans; folding a prefix gives the trace at that prefix's last sequence.
- **Generic.** A span's `label.kind` names a kind its search declared at `@search`. A client
  that does not know the kind renders it from `name`, `label.title`, `status`, `label.attrs_json`
  and `label.links`, so a new kind needs no protocol change. A kind's attribute schema is in the
  `ProtocolDocument` under `span_attributes.<orchestration>.<kind>`, so `attrs_json` decodes to
  a generated TypeScript type. An orchestration declares its kinds once, at `@search(spans=...)`.
  Its core policy carries the same declaration (`CorePolicy.span_kinds`), and
  `python -m entrypoints.protocol_schema` exports the built-in ones
  (`tests/server/test_protocol_schema.py` holds the committed schema to it). Every search
  labels the same kinds (`vibesys.search_spans`): `planning_call`, `workstream` (verdicts
  `passed`, `failed`; link `builds_on`), `agent_turn`, `gate` (verdicts `passed`,
  `failed_check`, `slower`) and `adoption` (verdicts `adopted`, `not_adopted`; attributes
  `candidate`, `metric_value`, `vs_baseline`). The adoption span is the operation that adopts the
  run's winner; a run that has not adopted yet has none, and `candidate` is null when the input
  itself stands. Clients read the run's candidates from these spans alone
  (`@vibesys/run-state/candidates`, folded linearly in events): a `workstream` is one attempt;
  attempts sharing `hypothesis_id` are one candidate; `idea` is what it tries; `probe` marks one
  that only looks; a passed one (the gates measured its kept code and none turned it down)
  names the `revision` it kept, which the adoption's `candidate` names when it is the best at
  the run's end; its `parent_revision` is its base, `base_round` the round whose idea made that
  base (null: the input), and its `builds_on` link names that workstream; `outcome` and
  `summary` are the implementer's own account of the attempt; and a gate's
  `failed` names the trusted check that rejected it with its progress (93 of 100 trials). A
  search that plays one path links each round to the round whose code it starts from, so its
  candidates form a chain. An `agent_turn` names its `agent` (its session's id, stable across
  replay and restart), `role`, `model` and `started_by`: the agents whose work started it, none
  when the search did, several for fan-in. Clients read the run's agents and who started whom
  from these turns alone (`@vibesys/run-state/agents`, constant work per event): an agent's
  status and activity are its latest turn's, which runs within its nearest labelled ancestor. A
  gate span covers one measurement; each of its stages (`build`, `accuracy`, `benchmark`) has its own `gate_started`/`gate_finished` events (`GateKind`) and its own log stream, whose `GateStage` owner names the span. The
  kinds are additive within `PROTOCOL_VERSION` 1: a client that does not know one renders it
  from its name and label.
- **Ids.** `flow:<path>#<n>` for a flow task, `op:<decision id>` for an operation, both plain
  strings. A span id is stable across replay and restart.
- **Contract choices.** `waiting_on[].kind` is `operation`, `slot`, `time` or `stop`. There is no
  `first`: a flow waiting on `ctx.first(a, b)` has one entry per source. `failure.kind` is
  `outcome` (for a `failed` span) or `interruption` (for a `stopped` span). There is no `defect`:
  a defect crashes the step that raised it, and that step is never committed, so no span event
  reports it. The run's failure event carries it instead. The run's `Finish` has no span:
  issuing it ends every open span, and the root span ends `done`, `failed` or `stopped` by the
  proposed result.
- **Agent turns.** An agent-turn span carries `invocation`, the invocation id the turn ran as.
  The runtime owns the mapping from that id to the span: the server stamps the envelope's
  `span_id` of every event whose `execution_id` ran as a recorded agent-turn span (the turn's
  output chunks, tool calls and results, activity, usage) and `turn_usage_recorded.data.span_id`
  with it. An event recorded before its span, or one outside a turn, carries none; events recorded
  before the field existed carry none. Clients never join the two, so a transcript reaches a span
  by `span_id` and never by invocation, name or time. A log stream's owner names its span in `span_id` when the writer
  knows it, and `logs.list` filters by it (see [desktop-connection.md](desktop-connection.md)).
- **Turn usage.** `turn_usage_recorded` is the settled spend of one agent turn, sent once when it
  finishes and keyed by `data.span_id`: `input_tokens` (cached ones included), the
  `cache_read_input_tokens` and `cache_creation_input_tokens` parts of it, `output_tokens` (reasoning
  included) and its `reasoning_output_tokens` part, each the turn's own increment. A field the
  provider did not report is absent (`null`), never zero, and a turn whose increment cannot be known
  (a resume with no baseline) reports none. Summing the events over spans gives the run's spend. It
  is not `usage_update`, whose `input_tokens` is the context window's current fill and is
  overwritten, not accumulated. The turn's model is the span's `model` attribute.
- **Typed replies.** A structured turn ends with the agent's raw JSON text, which stays in
  `agent_execution_finished.data.result`. When that text is a valid reply of the turn's role
  model, `data.reply` carries it as data: `kind` (the reply model's name; a role that may wait
  reports the result or the wait the envelope holds), `json_schema` (that model's JSON Schema) and
  `value` (the validated reply as JSON). A client learns which fields are text, choices or lists
  from `json_schema` and keeps no field list of its own; a reply that fails validation has no
  `reply`. `reply` is optional and additive within `PROTOCOL_VERSION` 1.
- **Evolution.** The three events, `RunSnapshot.spans`, `RunEvent.span_id`, `TurnUsageData.span_id` and
  `LogOwner.span_id` are additive within `PROTOCOL_VERSION` 1. A client that predates them counts
  the events as unknown kinds and ignores the new optional fields.

## Run overview

The run's header facts are one contract, `RunOverview` (`overview_version: 1`, defined once in
`vibesys.events` and used by both the core event and the wire). The run's `run_started` event
carries it, built from the task's input bundle (`vibesys.run.overview`):

- `objective.text` is the task's `OBJECTIVE.md` (Markdown) and `objective.metric` the
  task's headline metric (its `metric`, else the scalar benchmark result's metric).
- `objective.objectives` lists every axis candidates are ranked on, headline first (`name`,
  `direction` of `max` or `min`), as the run's metric space declares them (`objectives.toml`, or
  the operator's override); it is empty when the run declares none. A task with exactly two is
  one a client can plot as a trade-off. Units are the measurements': each gate span's `metrics`
  carries a row per reading the benchmark reported (`name`, `value`, `unit`, `direction`),
  headline first, beside the headline's `metric_value`; its `input_metrics` are the same rows for
  the input's own measurement (empty while it is unmeasured), so a client marks the input on every
  axis.
- `objective.correctness_check` is the argv of the accuracy check a candidate must pass.
- `benchmarks.evaluator` is the evaluator package the manifest pins (`name`, `version`). It is
  null for a source-tree evaluator, which has no version.

`RunSnapshot.overview` is that declaration (the server keeps it from the journal, live and on
replay), with `objective.unit` and `objective.direction` filled in from the run's own
measurements and objectives once it has them. It is null before the run started. Trajectories and
key numbers are not part of it. They are queries over the run trace's gate and workstream spans:
`@vibesys/run-state/overview` provides `gateTrajectory`, `bestVsBaseline` and `workstreamCounts`
beside `overviewFromSnapshot` and `foldOverview`. `query.performance` and `PerformanceContext`
stay unchanged for the frozen web run page. Gate tools' own versions are not measured today, so
the overview does not carry them.

## Waiting for capacity to return

A run paused on a provider capacity limit (`quota_paused`) resumes when the operator sends
`command.resume`. The request's `when` (`now`, the default, or `capacity_returns`) says how:

- `now` resumes at once. A wait for capacity in progress ends with
  `quota_wait_cancelled {provider, reason: resumed}`, then `quota_resumed {reason: operator}`.
- `capacity_returns` keeps the run paused until capacity should be back, then resumes it by itself
  with `quota_resumed {reason: wait_elapsed}`. The run server arms the same wait the `wait` policy
  uses (`vs_runtime`'s `capacity_wait`): the provider's reset time plus a five-second margin, or,
  when the provider gave none, the quota policy's `retry_seconds`; a turn that stops on the limit
  again after such a wait waits again by itself, so the run polls until the turn goes through.
  Each wait it arms emits `quota_waiting {provider, resumes_at}`, where `resumes_at` is the epoch
  second it resumes, `null` when it is polling without a reset time. The command acks
  `{action: resume, status: pending}`; a run not paused on a capacity limit answers the protocol
  error `capacity_wait_unavailable` and nothing changes. `fallback: true` cannot be combined with
  it (`invalid_request`).
- `fallback: true` resumes at once and switches the stopped turn to the quota fallback. It too
  needs a run paused on a capacity limit with a stopped turn still waiting to take it: otherwise
  it answers `capacity_wait_unavailable` and nothing changes. A fallback request that no turn
  took is dropped at the run's next resume.
- While a stop is pending, `capacity_returns` and `fallback: true` answer `run_stopping` instead,
  and nothing changes.
- When the last turn waiting on a quota stop is cancelled, a quota pause that nothing else ends
  (no `command.pause`, no timed resume) ends too: the run resumes itself, as it would after a
  restart, and a pending `capacity_returns` is dropped with it.
- `command.pause` during the wait cancels it (`quota_wait_cancelled {reason: paused}`): the run
  stays paused for the operator. A second `capacity_returns` replaces the first, emitting a new
  `quota_waiting`.

The run's state records the wait. A run resumed after its server ended does not stay paused on
a capacity limit, because the turns that stopped did not survive. It resumes, and the
operator's wait stands: the next turn that stops on the limit waits for capacity without asking
again, across any number of restarts, until such a stop takes the wait or a resume ends it. A run
the operator paused stays paused across a restart until the operator resumes it; a
`capacity_returns` asked for under that pause is dropped, since no turn survived to wait. `when`
is a new request field, so a client discovers support for it by `WP-CAPABILITY-PROBE`: an older
server rejects it as an unknown field.

## Operator controls

`command.control` carries one `control`, a `ControlRequest` (`vibesys.api`): the schema every
control ingress accepts, so a client and a headless run's inbox say the same things. Its
`action` is `steer` (`text`, a `target` of `kind` `run`, the default, or `role`, `attempt` or `item` with its `id`, and a `mode`: `next_turn`, the default, `standing`, which every later turn of the target receives until withdrawn, or `interrupt`, offered to a running turn of the target now and otherwise given to its next turn), `withdraw` (`withdraws`: the `control_id` of a standing steer), `pause`, `resume` (`fallback`, `when`, as `command.resume`), `cancel_attempt` (`attempt`: an attempt id), `cancel_item` (`item`: a work item id), `set_max_parallel` (`max_parallel`: an integer from 1 to the run's launch limit) or
`stop` (`mode`: `drain`, the default, or `cancel`, which also cancels the work in flight). Like
every protocol model it forbids unknown keys, and it has no field that reaches evaluation,
checkers, trust rules or resource partitions: `vs_runtime`'s `control_schema_fields` holds every
field to a reviewed allow-list of names and JSON types.

Every control carries a `control_id` the client chooses (1 to 128 letters, digits, `.`, `_` or
`-`). The same control sent again under its id returns the first ack and changes nothing; a
different control under a used id answers the protocol error `control_id_conflict`. The server
keeps answered ids for the life of the run session only, so the TypeScript transport does not
resend `command.control` after a dropped connection. The run records each control under its
id, so one sent both here and through a headless run's inbox under the same id is applied once;
one this channel's copy of was refused is set aside in the inbox with the same reason.
The server turns `command.pause`,
`command.resume`, `command.steer` and `command.stop` into the same request, named by their
envelope's `request_id`, and applies all five through one `RunControl.submit`, so they ack
alike. A single-action command that reuses a `request_id` with a different command is therefore
a different control under a used id, and also answers `control_id_conflict`. A refusal carries a typed reason that the server answers with its own protocol error. It
is a new request type, so a client discovers it by `WP-CAPABILITY-PROBE`. When a steer is submitted, the run refuses
only a target no later state could accept: a role the search does not declare answers the
protocol error `steer_target_unknown`, and an attempt that has ended answers
`steer_target_ended`. A resend gets the same answer. An attempt or item the run does not hold
yet is accepted, and resolved again when the run takes the steer. An accepted steer that later reaches no turn (its target ended first, or core dropped
it) is journaled as a `CONTROL` event with status `failed` and the text
`/steer dropped: steer <id> reached no turn: <reason>`. A withdrawal is refused only when its
`withdraws` names a control that is not a steer, or a steer that is not, or is no longer,
standing guidance (`withdraw_not_standing`); one naming a steer the run has not recorded yet is
accepted and checked when the run takes it, and journaled as `/steer dropped: withdraw <id> ended
nothing: ...` if it then names no guidance in force; a `vibesys control` withdrawal that ends nothing is also set aside in the run's inbox with that reason, so it never applies later. An interrupt-mode steer that a running turn accepted is journaled as a `CONTROL` event `/steer --interrupt` with status `consumed`. One that only the next turn got is journaled as `/steer: <note>`, the note saying that no running turn kept it. A
`vibesys control` pause, resume or steer is judged by the run's own rule (`vs_core`'s
`control_refusal`) against the pause the run is in, including a quota pause the run has not
committed yet; one the run still refuses when it commits (an input committed first closed or ended
the run) is set aside in the inbox with the run's reason as soon as that commit lands, and the run
goes on. A run that recorded a stop takes no pause, and a resume only until its ending commits: that resume lifts the stop (a `vibesys --resume` process records it first, before the search is asked for anything). A pause or resume acknowledged here
that the run then refuses, when it reads it or when it commits it, is journaled as a `CONTROL`
event with status `failed` and the text `/control refused: <action> <id>: <reason>`, and what it
did to the run's pause is undone: a refused pause is lifted, a refused `capacity_returns` or
`fallback: true` is dropped. Every request the run sets aside from its inbox is journaled, once, as a `CONTROL` event with status `failed` and the text `/control request <id> rejected: <reason>`. One exception: a request written as the run ended is refused to its poster directly and reads as rejected, but is not journaled when it was written after the run set its inbox aside, or when the run's process died before doing so. Once the run has ended, it answers every control still waiting: a pause or resume is refused as above, and a steer or withdrawal is journaled as dropped with the run's end. Any control sent after that answers `run_control_unavailable`, as one sent after the run session ends does. An accepted withdrawal is journaled as
`/withdraw: <withdraws>` with status `pending`. Accepted standing guidance is journaled with
status `active` as `/steer --standing: <text>`, and its end, once, with status `completed` as
`/guidance ended: standing guidance <id> ended: <reason>`, where the reason is `withdrawn`,
`owner-terminal` or `owner-cancelled` (its target ended), `run-terminal`, or `its attempt was reopened`.

The three operator actions apply the same way in every search, in the order they were sent
among the other controls. Core's rule decides whether the run takes one; it refuses an action
that could change nothing, or a `set_max_parallel` above the run's launch limit. When an action
is submitted, the run refuses only one no later state could take, with the protocol error
`action_attempt_ended` (the attempt has ended), `action_already_cancelled` (the item is already
cancelled) or `action_above_launch_limit`. Anything else is checked again when the run takes the
action. An accepted action is journaled as a `CONTROL` event with status `pending` and the text
`/<action> <subject>` (for example `/set_max_parallel 2`). Once the run has recorded it, it is
journaled again with status `completed` as `/<action> <subject> applied`. An action the run
refuses when it takes it is journaled as any refused control is:
`/control refused: <action> <id>: <reason>`. Every ingress words a refusal the same way, as
core does.

## Chat steer proposals

A run's chat agent can suggest a steer but cannot send one. Its only steering tool is
`propose_steer` (`text`, `target`, `mode`: the fields of a `SteerRequest` without a
`control_id`), built in `src/launch/chat_tools.py` beside its read tools, and no tool it has
sends, withdraws or controls anything: `test_the_chat_agent_has_no_tool_that_sends` holds the
tool list to that. The tool validates its arguments with the control schema's own models and
returns a `SteerProposalReceipt` (`vibesys.api`). When a chat turn's tool result carries one, the
server records a `steer_proposed` event on that chat thread (`chat_thread_id` on the event)
right after the `tool_result`. Its `data` is `{kind, proposal_id, proposal: {text, target, mode}}`;
`proposal_id` names the card (the turn and the provider's tool call id, or a fresh id when the
provider gives none). The event is data for a client's card: a steer reaches the run only when
the operator sends `command.control` with a `SteerRequest`, whose fields the card prefills.
Whether the run holds the proposed role, attempt or item is decided then, by the refusals above,
since only the run process knows its declared roles and live attempts.

## Framing and connection decisions

Each decision below carries a stable token. The conformance corpus (`tests/conformance/`, see
`tests/conformance/README.md`) tags every scenario with the tokens it exercises, and the corpus gate
(`clients/tools/scripts/check_conformance_corpus.mjs`) fails if any token here is never exercised or a
scenario tags a token that does not appear here. So this document cannot drift from the wire without
a test failing.

### WP-GRANULARITY: one frame carries exactly one protocol message

One transport frame is exactly one serialized protocol message. On the Unix transport a frame is one
JSONL line; `_write_message` serializes one model and appends `\n`
(`unix_jsonl.py:_write_message`). On the WebSocket transport a frame is one WebSocket message.

Batching is not framing. `event_batch` is one protocol message that carries many events in its
`events` list. The batch is a payload construct (`EventBatchMessage` in `protocol.py`), produced by
the server coalescing a watermark-consistent snapshot (`unix_jsonl.py:_stream`), and it crosses the
wire as a single frame on either transport. A transport never splits or merges frames. The server
may send one batch as several whole `event_batch` messages, one frame each, so no frame exceeds the
shared client cap (`protocol.py:EventBatchMessage.stream_messages_within`); folding them in order is
folding the batch. Both Unix JSONL and WebSocket use that projection, so transport choice cannot
change which checkpoint is deliverable.

An active-execution checkpoint can consume most or all of the cap even before its batch adds events:
assignments are user-authored and the list describes every concurrent execution. Whenever the
checkpoint and any one event cannot share a bounded batch, the event frames carry
`active_executions: null`, meaning "preserve the last complete checkpoint while this replay updates
it", plus `active_executions_checkpoint_id`, followed by ordered `active_executions_chunk` messages
with that identity. Their payloads concatenate to the JSON encoding of the generated
`ActiveExecutions` type. The persistent client stream owns reassembly for one connection generation.
Each deferred event frame folds exactly once into a private, serializable execution projection while
the public state retains its last complete execution checkpoint and statuses. After the complete
payload validates and its SHA-256 matches `checkpoint_id`, the stream emits one synthetic empty,
non-rebootstrap `event_batch`; folding it atomically publishes the staged execution projection and
the authoritative checkpoint without retaining or replaying the event frames. An ordinary live
transfer needs only the completed checkpoint. A rebootstrap transfer rebuilds the same state as the
unsplit authoritative batch inside that private transaction instead of reconciling into the
pre-replay fold. A checkpoint prefix is never passed to the run-state fold. Fragments require the
matching deferred batch immediately before their transfer; a new deferred or ordinary authoritative
batch supersedes an interrupted transfer and fences its late fragments. A gap, duplicate,
interleaved unrelated message, changed identity, changed cursor metadata, or content digest mismatch
is a protocol fault. A disconnect discards staging, so fragments from two connections cannot
combine. The assembled UTF-8 payload is capped at 16 MiB and a fragment at 64 Ki Unicode code points;
the 256-fragment generated-schema bound is the matching worst-case count. The server validates the
aggregate and its complete fragment projection before sending any event frame; an oversize snapshot
therefore produces a typed `stream_failed` error without advancing the peer's cursor. Every
serialized fragment still has to fit the transport byte budget.

### WP-FRAME-TYPE: text frames, UTF-8

Frames are UTF-8 text. On the Unix transport the bytes are UTF-8 encoded JSON. On the WebSocket
transport, frames are WebSocket text frames (opcode 0x1), not binary frames. Binary framing is
reserved for a future payload change and is out of scope here.

### WP-NEWLINE: the trailing newline is transport local

The `\n` the Unix transport appends is a framing delimiter for a byte stream, not part of the
message. A message-framed transport does not carry it. One WebSocket text frame is one message with
no trailing newline. A reader that strips a trailing newline before parsing is correct on both
transports; a reader that requires one is Unix specific and wrong.

A Unix request line is bounded like a WebSocket frame: the server buffers at most
`MAX_REQUEST_LINE_BYTES` (1 MiB, the gateway's inbound frame cap) of one line. A longer line draws one
failed response with diagnostic code `request_too_large`, its remaining bytes are dropped up to the
next newline, and the session reads on.

### WP-FRAMER: NewlineFramer is Unix only

`NewlineFramer` (`clients/libs/transport/src/newline-framer.ts`) reassembles messages from a byte
stream that may split a message across reads or pack several into one read. That problem exists only
on a stream transport. WebSocket delivers whole messages, so the framer does not run on the
WebSocket path. Framing lives in the transport adapter, below the shared client, so each transport
brings its own.

### WP-ROLES: one connection per role

The client opens three kinds of connection, and each maps to its own transport connection. This
mirrors the three sockets the Unix client opens today and needs no multiplexing.

| Role | Purpose | Lifetime |
| --- | --- | --- |
| control | requests and their responses (`command.*`, most `query.*`) | long lived; carries many sequential request/response pairs |
| subscribe | one `subscribe` then the event stream | one subscription; server streams until the peer goes away |
| chat | one `query.chat` on its own connection | one long-running request and its single response |

On the Unix transport the control connection reads requests in a loop and writes one response per
request (`unix_jsonl.py:_ClientSession.handle`); a `subscribe` request takes over its connection
and never returns to that loop (`unix_jsonl.py:_stream`). A transport must preserve this: a
subscription owns its connection for the connection's life, and chat is isolated on its own
connection so a long agent turn does not block control traffic behind it.

### WP-SUBSCRIBE-ACK: subscribe is acknowledged before any batch, even on failure

The server answers an accepted `subscribe` with a `subscribed` message carrying `run_id` and
`latest_sequence` before it sends any `event_batch` (`unix_jsonl.py:_stream`). If the bootstrap
replay then fails, the server has already sent `subscribed` and reports the failure as a
`protocol_error` frame (`unix_jsonl.py:_write_stream_error`). So a client always sees `subscribed`
first on a dial the server accepted, and a bootstrap failure is a stream error after the ack, never
a rejected dial. A transport must not reorder or drop the ack.

The acknowledged identity and the `run_id` stamped on snapshots and events name the same run.
Projection state latches its first non-empty stamped identity and rejects later data for another
run. Empty or absent ids are legacy unknown values, not a request to replace an established owner.

### WP-PROTOCOL-ERROR: a protocol error is an in-band frame then close

`ProtocolErrorMessage` is a normal framed message (`type: "protocol_error"`). The server writes it in
band and then closes the connection (`unix_jsonl.py:_write_stream_error`). It is not expressed as a
transport close code. This is not cosmetic: the client suppresses its own disconnect callback after
it reads a `protocol_error`, because the drop that follows is the expected consequence of the error,
not a separate outage. A WebSocket transport that mapped a protocol error onto a close code instead
of an in-band text frame would change error semantics with no compile-time signal, so it must send
the frame and then close.

### WP-DISCONNECT: disconnect detection is transport specific and needs a liveness signal

The Unix server notices a gone client through the connection's non-blocking `peer_closed` check
(`vs_sim/network.py:Connection.peer_closed`; on a real socket a `recv(1, MSG_PEEK | MSG_DONTWAIT)`
that returns empty on FIN), made only while the stream is idle (`unix_jsonl.py:_stream`). A response
written after the client hung up ends that client's session and nothing else
(`unix_jsonl.py:UnixJsonlServer._serve`). That probe has no WebSocket equivalent, and a WebSocket that dies without
a close (a slept laptop, a killed tab) leaves the server with no FIN to read. The disposition:

- A clean browser close (tab closed, navigation) sends a WebSocket close frame, which the gateway
  treats exactly as the Unix FIN: the subscriber count falls and teardown proceeds.
- A hard-dead peer (network partition, power loss) is reaped by a server-initiated WebSocket ping
  whose pong never arrives. Browsers answer a ping automatically below the JavaScript layer, so this
  needs no application protocol.
- A frozen or throttled tab still answers pings while reading nothing, so ping liveness cannot see
  it. That case is handled by the send-side write deadline below, and by the optional client
  heartbeat (`WP-HEARTBEAT`).

The bounds are stated, not inherited. The `src/server/transport/websocket.py:WebSocketLimits`
dataclass names every one of them. The gateway owns the keepalive schedule and deadlines; its
`src/server/transport/websocket.py:WebSocketGateway._serve_until_stopped` disables the dependency's
automatic keepalive and passes the remaining library bounds explicitly to `serve()`, so a
`websockets` upgrade cannot move a bound a subscriber depends on. Three gateway-owned bounds
compose into the keepalive ceiling:

| Bound | Value | Role |
| --- | --- | --- |
| `ping_interval_seconds` | 20s | Idle period before the server probes the peer |
| `write_deadline_seconds` | 40s | Maximum time for the Ping write to pass flow control |
| `ping_timeout_seconds` | 20s | How long a pong may be outstanding before the peer fails |
| `close_timeout_seconds` | 10s | Library bound for an ordinary closing handshake after handler teardown |

A peer that stops answering is aborted after at most 80s: 20s before the probe, at most 40s for its
Ping write, then at most 20s for the Pong. When the Ping write drains promptly, the nominal bound is
40s. The abort moves the connection to `CLOSED`, which is what the stream loop polls for, so the
subscription is released one `_DISCONNECT_POLL_SECONDS` later and a non-detached run may then finish
after `RECONNECT_SETTLE_SECONDS`. A peer that sends a FIN needs none of this. The separate close
timeout still governs graceful handler teardown; the keepalive deadline aborts directly because a
close frame is another write and cannot safely be the escape hatch for a blocked Ping write.

Flow control has two separate bounds in opposite directions, and conflating them is the easy
mistake. `max_queue`, `(32, 8)`, bounds frames arriving *from* the peer. `write_limit`, 32 KiB, is
the send-side high-water mark, and it is the one that carries the contract below.

**The send-side overflow policy is to stall the producer, not to drop events and not to disconnect
on the first slow read.** Past the high-water mark the producing coroutine suspends in the library's
`drain()` until the peer catches up. This is deliberately the same shape as the Unix path, where
`src/server/transport/unix_jsonl.py:_ClientSession._write_message` does a blocking `send` of the
whole line, and it is what preserves burst batching on both transports: a stalled stream loop is not
reading the journal, so the next `subscription_checkpoint` coalesces the whole backlog into one
`event_batch` instead of one frame per event. Slow consumers get fewer, larger batches rather than
lost events.

The stall is bounded. A single frame may stall for `write_deadline_seconds`, the nominal keepalive
response window (40s) by default; past that the peer is treated as gone and its socket is aborted.
That bound is not optional decoration. `websockets` ends *every* write in `drain()`. The gateway
therefore applies the same deadline to application frames, its owned Ping, and its final close. A
peer that neither drains its socket nor answers pings cannot block the very keepalive that reaps it.
Without the Ping-write deadline such a peer was never reaped at all, its subscription was never
released, and a non-detached run never exited. The socket is aborted rather than closed on deadline
because a close frame is itself a write and would re-enter the same stalled `drain()`.

The Unix path has no equivalent deadline: its blocking write stalls the handler thread
indefinitely, which is pre-existing behavior this contract records rather than changes. Its
gone-client probe fires only while the stream is idle. So the two transports agree on the stall and
differ on the ceiling. A remote client that reaches the Unix transport through the relay
gets a ceiling at the relay instead (see [The relay](#the-relay)).

### WP-HEARTBEAT: the application heartbeat is optional and probe advertised

Because browsers do not expose WebSocket ping and pong to JavaScript, a client that wants to detect a
dead server (as opposed to the server detecting a dead client) needs an application-level signal. The
frame shape is reserved here so both ends agree before either implements it: an optional
`heartbeat_ms` field on `SubscribeRequest`, capability probed like `tail` and `store_id`
(`WP-CAPABILITY-PROBE`). A server that has the field emits a periodic keepalive frame the client can
time out against; a server that predates it rejects the field, and the client falls back to no
application heartbeat. This reservation only fixes where the frame rides so it is additive.

No server-side heartbeat frame exists, and the server does not need one. The direction this contract
governs, the server detecting a dead client, is served entirely by protocol-level pings, because a
browser's own WebSocket implementation answers them below the JavaScript API (`WP-DISCONNECT`). Only
the opposite direction, a client detecting a dead server, would use the reserved field, so it stays
reserved and unimplemented on both transports. `tests/conformance/scenarios/heartbeat-probe.json`
records the reservation: the field is rejected today, and that rejection is the capability probe
working, not a gap. It records rather than pins, because no runner executes it yet
(`tests/conformance/README.md`).

### WP-CAPABILITY-PROBE: capabilities are probed by rejection, not advertised

There is no capability advertisement. A client discovers whether the server supports an optional
request field by sending it and reading the result: acceptance means the field works, a
`extra="forbid"` rejection means the server predates it. The subscribe fields `tail` and
`store_id` are governed this way (`SubscribeRequest` in `protocol.py`; the client's
probe-and-fallback in `clients/libs/transport/src/persistent-event-stream.ts`), and so is
`command.resume`'s `when` (see [Waiting for capacity to return](#waiting-for-capacity-to-return)).

The recorded consequence, accepted deliberately: a browser resume that carries `store_id` against a
server that lacks it costs one extra speculative dial (the rejected probe, then the cursor-only
resume). Probe-by-rejection is kept rather than adding an advertisement frame, because the number of
optional fields is small and an advertisement is a new frame both transports would have to reproduce.
Revisit if the option set grows.

### WP-REBOOTSTRAP: a store swap or tail overflow restarts the replay with a non-contiguous sequence

Two conditions make the server abandon a client's cursor and send a fresh bootstrap batch instead of
a continuation:

- The run attached its durable event store after the client subscribed, so the batch's `store_id`
  differs from what the client folded (`unix_jsonl.py:_stream`). Sequences are only comparable within
  one store.
- More live output landed in one wait than the `tail` bound was willing to replay
  (`unix_jsonl.py:_stream`).

In both cases the next `event_batch` sets `rebootstrap: true`, supersedes the client's fold rather
than extending it, and its `through_sequence` is not the client's cursor plus one. The marker means
this is a second bootstrap on an already-open subscription. A rebootstrap that the WebSocket gateway
splits to fit its send cap carries the marker on its first frame only. The first batch of a reconnect
remains `rebootstrap: false`, even when a changed `store_id` makes that new connection's first batch a fresh
fold. A client keys continuation on the dial context, the marker, and `store_id`, not sequence
contiguity. A transport carries these batches unchanged; the rebootstrap decision is server logic,
not framing. An in-loop rebootstrap restarts from what the stream itself delivered (its cursor and
the store that cursor numbers), never from the subscribe request's cursor, so a swapped store is
replayed from its start whether or not the request named a store.

Rebootstrap is the sole projection transition allowed to adopt a different `run_id`. Within any
ordinary batch, event identity also guards the batch-level `active_executions`,
`through_sequence`, and `history_after_sequence`: if an event is rejected as foreign, those
metadata do not apply. A same-run rebootstrap may retain snapshot state that the replay tail cannot
reconstruct; a changed or unknown identity must start without state owned by the prior run.

### WP-IDENTITY: the issuing connection is identified by a stable client id

Two clients on one run is the configuration the browser port creates, and the server must be able to
say which connection issued a request (for #840's acknowledgment attribution, and for the two-client
conformance scenario). This is the optional protocol field `client_id`. A frontend transport
generates one stable id for its client instance and carries it on control requests, subscriptions,
and dedicated chat requests, including requests sent after a reconnect. `Response`,
`SubscribedMessage`, and `ProtocolErrorMessage` reflect it. An empty id is the backward-compatible
default for a peer that predates attribution.

The field is defined in `src/server/api/protocol.py` and generated into the TypeScript bindings. The
corpus reserves a two-client scenario that asserts independent attribution with one Unix and one
WebSocket subscriber live in one process.

## The relay

A remote client (the desktop over SSH) reaches a run's Unix transport through the relay, the
relay core `src/server/relay.py` that a host helper run channel becomes after its
`run.connect` (see [desktop-connection.md](desktop-connection.md)). It is not a third transport.
The client opens one run channel per protocol connection over its own ssh exec channel, so
`WP-ROLES` holds unchanged: a control, subscribe, or chat connection is one helper process. The
relay connects to the socket and copies bytes in both
directions between its stdin/stdout and the socket. It does not parse or re-frame: every Unix
transport decision above (`WP-GRANULARITY`, `WP-NEWLINE`, `WP-PROTOCOL-ERROR`, and the rest) holds
through it, and the client keeps its `NewlineFramer` (`WP-FRAMER`) because its side is still a byte
stream. The conformance runner `tests/conformance/runners/relay.json` replays the Unix
scenarios through a real helper's run channel to hold it to that.

**Ends.** A relayed channel ends with one exit status per outcome, and, whenever the status is not
0, one JSON line on stderr: `{"outcome": ..., "exit_status": ..., "detail": ...}`
(`src/server/host_api.py:ChannelEnd`, in the helper's generated schema). The outcomes are
`src/server/host_api.py:ChannelEndOutcome`:

| Status | `outcome` | Meaning |
| --- | --- | --- |
| 0 | `client_closed` | stdin reached end of stream, or stdout was closed: the client went away. No stderr line. |
| 3 | `server_closed` | The server closed the connection. Everything it sent was relayed first. |
| 4 | `run_gone` | No socket file, or nothing listening on it: the run is not there. |
| 5 | `connect_denied` | The socket exists but this user may not connect to it. |
| 6 | `connect_failed` | Connecting failed another way, including the connect timeout. |
| 7 | `server_stalled` | A write toward the server made no progress within the write deadline. |
| 8 | `client_stalled` | A write toward stdout made no progress within the write deadline. |
| 9 | `server_failed` | A socket read or write failed with an error other than a close. |
| 10 | `client_failed` | A stdin read or stdout write failed with an error other than a close. |
| 11 | `server_unresponsive` | The socket sent nothing within the read deadline: the server is wedged. |

Status 1 stays an uncaught error and 2 a usage error. So a client can tell "the run is gone" (4)
from "the transport is broken" (5, 6, and the stalls) from "the server ended this connection" (3)
without parsing text.

On stdin end of stream the relay closes the socket, which the server reads as the client's FIN
(`WP-DISCONNECT`), so a closed ssh channel releases a subscription exactly as a closed Unix client
does. Replies still in flight toward a client that closed its stdin are dropped; a client keeps
stdin open for a connection's whole life. In the other direction, a write toward the server that
finds it closed does not end the relay by itself: the server may have written a final
`protocol_error` frame before its close (`WP-PROTOCOL-ERROR`), so the relay ends only when its
socket read reaches end of stream, after relaying that frame.

**Deadlines.** `src/server/relay.py:BridgeLimits` names every bound:

| Bound | Value | Role |
| --- | --- | --- |
| `write_deadline_seconds` | 40s | How long one write toward either the server or stdout may make no progress |
| `connect_timeout_seconds` | 5s | How long connecting to the socket may take |
| `read_deadline_seconds` | 7d | How long the socket may send nothing before the relay ends |

The write deadline is the hop's answer to the Unix path having none. A half-open ssh hop (a slept
laptop, a partition) stops draining the channel; once ssh's window and the pipe fill, the relay's
stdout write stops progressing, and after 40s the relay closes the socket and exits with
`client_stalled`. The server then sees an ordinary client FIN, so a dead remote client pins server
resources for no longer than the WebSocket gateway's `write_deadline_seconds`, which has the same
value. The client-facing hop is idle-reaped by ssh's own liveness instead: when sshd closes the
channel the relay reads end of stream on stdin.

The server-facing hop (the Unix socket) has no such liveness: `AF_UNIX` has no keepalive, so a
wedged server that neither sends nor closes would hang the relay forever without its own bound. The
read deadline is that bound, checked only against the socket's read (not stdin's, which ssh already
reaps): after `read_deadline_seconds` of nothing read from the socket, the relay exits with
`server_unresponsive`. A provider capacity pause can leave the socket legitimately silent for
*unbounded* time: `[agent.quota]`'s shipped default policy (`pause`) waits for the operator with no
bound at all, and the self-resuming policies wait without a bound too unless `wait_seconds` is set
(`vibesys.config.QuotaCfg`). No finite deadline is "longer than every legitimate wait" the way the
write deadline is, so this one instead draws the line at a week: long enough that an operator who
left a run paused past it is unlikely to still be waiting on this particular connection, short
enough to eventually recover a genuinely wedged server. Tearing down a relay that outlives it only
ends one viewer's connection, not the run itself.

## The conformance corpus

The framing decisions above are enforced by one shared fixture corpus at `tests/conformance/`, read
by both conformance suites: the client-side fold suite (#812) and the server-side scenario suite
(#811). Neither side keeps a private copy. The corpus has two parts:

- `tests/conformance/events/` holds one fixture per `EventType` (the event-kind enum in the generated
  schema). The corpus gate enumerates the enum from
  `clients/libs/contracts/src/generated/protocol.schema.json` and fails if a kind has no fixture, so
  a new event kind cannot land without a fixture.
- `tests/conformance/scenarios/` holds the connection scenarios (bootstrap, resume, rebootstrap,
  capability probes, protocol error, and so on), each tagging the framing decisions it exercises.

See `tests/conformance/README.md` for the layout and how to run the gate, and
`tests/conformance/FORMAT.md` for
the fixture and scenario formats. The replay runners that execute the scenarios against a live
transport land with the transports themselves (888b for the client fold runner over the Unix
transport, #811 for the two-transport scenario); this document and the corpus define what they must
reproduce.
