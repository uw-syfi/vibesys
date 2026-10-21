# Run transport

This page owns how a viewer (the desktop app) sees and steers runs on a host: what a run writes to
disk, the one gateway that serves those files, the one connection that carries them, and the event
model viewers read. It is written as the design. Parts not built yet are marked in the
[status table](#status). It replaces [`wire-protocol.md`](wire-protocol.md) and
[`desktop-connection.md`](desktop-connection.md), which describe the stack being retired.

## Goals

- Remote runs are first class. The Electron app is only a viewer.
- A remote run can be iterated on quickly: drain its agents to a good stopping point, update the
  code, relaunch the same run, repeat, and watch it throughout.
- Opening a run costs time proportional to its spans, not its events. Scrubbing is local and
  instant; live updates arrive as they are written.
- The core event model names no search strategy. Rounds, evolutionary search and anything else are
  span kinds a strategy declares.
- One simple scheme. Client and server run the same code. Every cross-language contract is a
  `.proto` file.
- No long backward compatibility. Nightly releases keep the old reads they promise (see
  [Versions](#versions-and-upgrades)) and nothing more.

## Status

Everything is unbuilt except the file inbox (`libs/vs-project/src/vs_project/_control_inbox.py`) and
the log streams.
The migration phases are in [Migration](#migration).

| Part | Status | Phase |
|---|---|---|
| `vibesys.store.v1` and `vibesys.api.v1` protos, codegen, CI (`buf breaking`) | unbuilt | 1 |
| Go gateway `vibesysd`: `Hello`, `Tail` | unbuilt | 1 |
| Electron provisioning of the gateway; one HTTP/2 connection per host behind a per-host switch | unbuilt | 1 |
| Converter follower: live JSONL journal into `run.log` (`LegacyEvent` records) | unbuilt | 1 |
| Store records for every event kind ([inventory](#event-inventory)) | unbuilt | 2 |
| Skeleton keyframes, `run.head`, `~/.vibesys/runs/<run_id>` link | unbuilt | 2 |
| `Skeleton`, `GetBlobs`, `TailStream`, `WatchRuns`; new Run screen | unbuilt | 2 |
| Conversion of existing JSONL runs and `store.json` envelopes | unbuilt | 2 |
| Core state as generated types, `state.pb`, native `run.log` writes | unbuilt | 3 |
| Intents as records (`IntentState`), drain/park, relaunch epochs | unbuilt | 3 |
| Chat sessions as processes with their own log; `HostCall` | unbuilt | 3 |
| Generated frozen value classes from `.proto` (our own protoc plugin) | prototype only | 3 |
| Control inbox (files, ordered, durable, idempotent by request id) | built | |
| Log streams (`vs_logs`) | built; moves under the run directory | 2 |

## Principles

1. **The run process only writes files.** It never serves a socket. Anything a viewer can see is in
   its log. A parked or dead run is read exactly like a live one.
2. **A writer and its readers share one machine.** Only evaluation jobs leave it. File change
   notification (inotify) works for same-host writes even on an NFS root; the measured latency
   is about 0.4 ms.
3. **The gateway has no product logic.** It tails files, serves them, writes intents and launches
   processes. Everything that needs product knowledge runs as a `vibesys host <method>`
   subprocess of the host checkout.

## What a run writes

One directory per run, opened only through `vs-project`'s `Project`.

| Path | What | Writer | Mutability |
|---|---|---|---|
| `run.log` | the history: ordered `vibesys.store.v1.Record`s | the run | append-only |
| `blobs/<sha256>` | large content: transcripts, diffs, tool output, prompts | the run | immutable, content-addressed |
| `streams/<id>` | log streams (build output, stage output, narration) | the run | append-only |
| `keyframes/<seq>.pb` | the skeleton at a log sequence | the run, at its checkpoints | immutable |
| `run.head` | tiny status and headline for dashboards | the run | replaced atomically |
| `state.pb` | core's snapshot; records the last log sequence it covers | the run | compare-and-swap |
| `inbox/` | intents posted by viewers | the gateway | one file per request |
| `sessions/<id>/` | a chat session: its own `session.log` and inbox | the session process | as the run |

**Run discovery.** The run publishes `~/.vibesys/runs/<run_id>`, a symlink to its run directory,
written through `Project`. The gateway lists that directory and never rebuilds the project layout.

**`run.log` framing.** An 8-byte magic `VSRLOG01`, then per record: a uvarint payload length, a
4-byte little-endian CRC32C of the payload, and the payload (an encoded `Record`). A tail that ends
inside a record is a torn tail: the reader waits. A complete record whose CRC does not match is
corruption: the reader stops and reports it.

**Commit and recovery.** One commit appends its log records, then compare-and-swaps `state.pb`,
which records the last log sequence it covers. On start, recovery drops log records past that
sequence, so the history never shows something core does not hold. (Making `run.log` core's own
event-sourced journal instead was considered; see [Decisions](#decisions-and-alternatives).)

**Blobs and streams.** A record never carries large content inline. It carries a `BlobRef` (hash,
size, media type) and the client fetches by hash, caching in IndexedDB forever because the content
cannot change. Log streams are append-only files read by offset.

**Skeleton keyframes.** The skeleton is what a client holds in memory to scrub: the span table
with each span's versions by sequence (status, verdict, attrs), the metric series, the stream
index and the epoch list. The run writes a keyframe at its checkpoints (a bounded number of
records, a lifecycle change, an epoch end). The `Skeleton` call returns the latest keyframe plus
the log records after it, so opening a run reads a keyframe, not the log.

**`run.head`.** The headline a list row draws: lifecycle, strategy name, objective and best result
against the input, counts of work units, token and cost totals, a bounded point list for the tile
chart, and whether the run needs its user. The run writes it; no reader folds a journal for it.
This deletes the second (Python) copy of the summary fold.

## Intents and epochs

**Intents** are pause, drain, resume, stop, steer, withdraw, cancel and set-parallelism, and chat
messages. A viewer posts one to the run's inbox through the gateway. The run reads the inbox at
its control poll, applies each request once, and records an `IntentState` (`applied` or `refused`,
with the reason) in its log. A request posted while the run is parked or down is applied after it
relaunches; commands are durable. The ordering, idempotence and rejection rules of the inbox
are unchanged.

**Drain and park.** Drain finishes the agent turns in flight, starts no new work, and cancels
in-flight evaluations. Evaluation jobs are idempotent, so they are rerun after relaunch. The process
then exits with lifecycle `parked`. Pause is the same hold without the exit (see
[open question 7](#open-questions)).

**Relaunch** starts the same run as a new epoch from the host checkout. The `EpochStarted` record
records the tree hash and a dirty flag, so the timeline shows each epoch boundary and which code
ran in it. A run restarts from `state.pb`; its open spans from the previous epoch are resumed or
ended by the same rules a restart after a code change uses today
([flows.md](flows.md#the-run-trace)).

**Chat sessions** are separate processes with their own `session.log` and inbox. They are not the
run: a session reads the run's files, its answers and token streaming are appends to its own log,
and a steer it proposes is data in that log that the operator may send as an intent. A generic
`Attach` bidirectional byte stream to a socket a session owns is reserved for future byte-level
interaction (terminals). The run never serves one.

## The gateway and the connection

`vibesysd` is a statically linked Go binary, one per host per user per API-schema hash, kept in
`~/.vibesys/gateway/<apihash>/`. The app cross-compiles it from its own tree and copies it on first
connect; a release build later downloads the released binary instead. It is stateless over disk.

- **Transport.** One HTTP/2 connection per host, speaking gRPC. Remote: `ssh host vibesysd connect`
  pipes its stdio to the gateway's Unix socket, and Electron main runs Node `http2` over that
  duplex. Local: main dials the socket. Requests are client-to-server unary; updates are
  server-to-client streams.
- **Renderer.** The renderer makes two generic calls through main, `unary(method, bytes)` and
  `stream(method, bytes)`. Main owns the connection, reconnects, and re-issues streams from a
  sequence. There is no per-role channel, relay, session limit, line framing, byte credit or channel
  end code.
- **Calls.** The set is small and each call is generic over runs:

| Call | Kind | What |
|---|---|---|
| `Hello` | unary | exact API-schema-hash check; returns host facts |
| `WatchRuns` | stream | `run.head` of every linked run, then changes |
| `Skeleton` | unary | latest keyframe plus log records after it |
| `Tail` | stream | log records from a sequence, then live |
| `GetBlobs` | unary | blobs by hash |
| `TailStream` | stream | a log stream from an offset, then live |
| `PostIntent` | unary | writes the run's (or session's) inbox |
| `Launch` | unary | starts or relaunches a run |
| `HostCall` | unary | `vibesys host <method>` subprocess; protobuf on stdin and stdout |
| `Attach` | bidi | reserved |

The gateway reads records as framed bytes; it reads protos only for `run.head` and its own API.

## Versions and upgrades

Two version domains, with different rules.

**Wire (`vibesys.api.v1`).** `Hello` requires an exact API-schema-hash match. There is no
compatibility and no feature flag. Because the app ships the gateway from its own tree, the two
match by construction; a mismatch means a stale copy on the host, and the app replaces it.

**Store (`vibesys.store.v1` and core state).** Additive changes only, enforced by `buf breaking`
in CI. New code must read data written by the last 7 days of nightlies; a CI test resumes saved
runs from each. A semantic break bumps the package version and ships one upgrade function. A newer
writer is refused by name, never read as if it were older.

**Development mode.** The laptop tree builds the UI and the gateway; the host checkout runs the
runs. They may diverge as long as store changes stay additive. Sync pushes the laptop tree,
including uncommitted edits, to the host checkout.

## Contracts

Everything that crosses a process or is persisted is a `.proto` message, validated with
protovalidate in the same file. Codegen: protobuf-py (bufbuild) for Python, protobuf-es for
TypeScript, protoc-gen-go for Go. The core value types in `vs-core` keep their functional design
(frozen, hashable, closed sum types) and will be generated as frozen classes from the same
`.proto` by our own protoc plugin (prototype only). Pydantic contract models and the JSON Schema
pipeline are deleted at cutover.

## The event model

A run's history is a sequence of `Record`s. Core names no strategy.

- **Spans** are the unit of work: an id, a parent, links, a status over time, and typed attrs.
  The runtime already records them for every flow task and operation
  ([flows.md](flows.md#the-run-trace)); a strategy labels them with kinds it declares at
  `@search(spans=...)`. Each kind's attrs are a proto message. The `EpochStarted` header carries
  the descriptors, so a client that does not know a kind renders it from its descriptor.
  Strategy-specific event kinds become span kinds.
- **Metric points** are numbers over time: a series name, an optional span, a value. A series is
  declared once with unit, direction and role.
- **Stream refs** and **blob attachments** point at content kept outside the log.
- **Lifecycle** is `starting{stage,progress} | running | draining | parked | ended{outcome}`.
- **Intent states** record what the run did with each intent.
- **Epoch started** opens each epoch with the checkout, run description and declarations.
- **Notice** carries a human-facing warning or failure (a proposal; see
  [open question 6](#open-questions)).

The client holds the skeleton and scrubs locally: the state at sequence `s` is a lookup in the span
versions and series, not a replay. Content is fetched lazily by hash.

The next section maps every current event kind onto this model and proposes the records.

## Event inventory

### Sources

The 57 kinds are the files in `tests/conformance/events/` and the `EventType` enum in
`clients/libs/contracts/src/generated/protocol.schema.json`. Producers are in `src/vibesys/run`
(the commit observer, `integration.py`, and the agent, evaluation and git event adapters) and
`src/server` (journal, controller, chat manager, execution). Viewers are `clients/libs/run-state`,
`clients/features/run-page`, `clients/features/run-chat` and the server summary, digest and usage
folds (`src/server/run_summary.py`, `run_digest.py`, `run_usage.py`).

How to read the table:

- **Target** is where the data lives afterwards. `span:<kind>` names the span kind and its attrs.
  `lifecycle`, `intent`, `epoch`, `metric`, `stream`, `blob`, `notice`, `chat` and `delete` are as
  in the model above.
- **Viewer today** says what reads the kind. "None" means the kind is folded or journaled and no
  screen shows it. A client fold that nothing displays is dead weight, so several of these are
  deletions.
- **Size** is `S` (under about 1 KiB), `M` (a few KiB), or `L` (can be large; the content goes to a
  blob and the record carries its ref).

### Run lifecycle and startup

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `server_started` | server journal | delete | none | S | Artifact of the old server process. `EpochStarted` and `Lifecycle.starting` replace it. |
| `server_ready` | server runtime | delete | e2e fakes only | S | `Lifecycle.running` when startup ends. |
| `run_started` | `vibesys.api` session, server | epoch | live page fold (objective, metric), digest, run map | M | Strategy name, input, max rounds, expected roles and `RunOverview` go in `EpochStarted.run` (`RunDescription`). Objective text is a blob. Unit and direction are in the series declarations, so the server no longer back-fills them into a snapshot. |
| `run_startup_progress` | `vibesys.run.resources` | lifecycle | startup bar (`run-session`), startup timing words (`scoped-logs`) | S | `Lifecycle.starting{stage, state, condition, elapsed, progress}`. One record per step; bounded by the number of image-build steps. |
| `workspace_root_probe` | `slurm_probe_events` | span:`environment_probe` | none (ignored by the fold) | S | Runtime-declared kind. Attrs: `state`, `job_id`, `pending_reason`, `waited_seconds`, `queue_timeout_seconds`, `failure`, `attempt`, `retry_in_seconds`. A `failed` probe ends the run with `RunFailure.kind = environment_unavailable`. |
| `run_status_changed` | server controller, journal | lifecycle | `CoreState.status`: header, recent-status words | S | `starting\|running\|pausing\|paused\|stopping\|stopped\|completed\|failed` becomes `Running{hold}`, `Draining{goal}`, `Parked`, `Ended{outcome}`. `previous` is derived. |
| `run_finished` | controller, runtime, session | lifecycle | fold terminates run; digest closing | S | `Ended{outcome = finished}`. |
| `run_failed` | controller, runtime, session | lifecycle | fold terminates run; digest closing; failure text | S | `Ended{outcome = failed, failure = RunFailure}` (kind, reason, workstreams started and budget, candidates kept, adopted candidate). |
| `run_interrupted` | controller, runtime | lifecycle | fold terminates run | S | `Ended{outcome = interrupted, reason, signal}` when the process caught the signal. A process that cannot write its own death is detected by the gateway (open question 2). |
| `run_ending` | `vibesys.run.core_run` | lifecycle | host summary (`with_ending`): end reason, resumable | S | Merged into `Ended`: `reason`, `summary`, `resumable`, `uncompared`. `rounds_finished` is dropped (count ended workstream spans). |
| `configuration_failed` | server runtime, read model | lifecycle, or launch error | transcript line; diagnostics | S | Raised before a run exists: it is the `Launch` or `HostCall` error. If a run directory exists it is `Ended{failed, kind = configuration, code, stage, message, exit_code}`. |

### Agent turns and their content

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `invocation_started` | server execution | delete | none | L | Legacy duplicate of `agent_execution_started`; the journal already rewrites it into that kind. |
| `invocation_finished` | server execution | delete | none | L | Same, for `agent_execution_finished`. |
| `agent_execution_started` | server execution, `run.integration` | span:`agent_turn` attrs, blob | agents view, step narrative, usage join (`ExecutionStart`) | L | The span already exists. Add attrs `stage`, `attempt`, `provider`, `model`, `reasoning_effort`, `harness_version`. `system_prompt` and `user_prompt` are blob attachments (`system_prompt`, `user_prompt`). The execution id is the span id, so the usage join disappears. |
| `agent_execution_activity_changed` | server execution | delete | active-turn word (`activeExecutions`) | S | Derived by the client from the tail of the turn's transcript stream (last entry kind and tool). A past sequence needs no activity. |
| `agent_execution_finished` | server execution, `run.integration` | span:`agent_turn` end attrs, blob | turn rows (typed reply fields), idea outcomes | L | `reply` (kind, value) becomes `Any` attrs with its descriptor in the epoch header, replacing `json_schema`. `result` (the raw text) is a blob attachment (`raw_result`). `error` is the span's failure. |
| `agent_output_chunk` | server execution, `run.agent_events` | stream (`transcript`) | step narrative, turn rows | M | Transcript entries `assistant`, `analysis`, `prompt`, `diagnostic`. The channel `tool` is dropped (typed tool entries replace it). `status` (progress, label, elapsed, tokens) is presentation and is deleted. |
| `tool_call` | `run.agent_events` | stream (`transcript`) | turn rows (tool cards) | M | Entry: tool, call id, args. Args over the inline cap go to a blob. |
| `tool_result` | `run.agent_events` | stream (`transcript`), blob | turn rows | L | Entry: tool, call id, error flag, summary. `content` and `payload` (command stdout and stderr, JSON) are a blob when over the cap. |
| `todo_update` | `run.agent_events` | stream (`transcript`) | none | S | Entry kind `todos`. Add a screen first, or delete. |
| `usage_update` | `run.agent_events` | delete | none (`CoreState.usage` is unread) | S | Context fill per tool call. High rate, no consumer. A `context_fill` metric series can return when a screen needs it. |
| `turn_usage_recorded` | `run.agent_events` | span:`agent_turn` end attrs | usage view, run summary, global Usage page, workstream usage | S | `TokenUsage` attrs: input, cache read, cache creation, output, reasoning, cost, duration; unreported is absent, never zero. Summing over spans gives the run's spend. The `span_id` stamping on events is gone. |
| `phase_started` | server execution, `run.integration` | delete | run map only; the Run screen reads no phase | S | Each phase is an agent turn or a workstream span. |
| `phase_finished` | server execution, `run.integration` | delete | same | S | |
| `subprocess_output` | `run.evaluation`, `measurement_events` | stream (`log`) | transcript "subprocess" rows | L | One stream per process, owner the span that ran it, redacted at write time. |
| `output` | server journal | stream (`log`) | none | L | The run process's own stdout and stderr is one stream, `narration`. |

### Capacity and providers

A turn that stops on a provider capacity limit is a span, not an event pair. Its duration is the
wait, so `waited_seconds` is no longer a field.

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `quota_paused` | `run.agent_events` | span:`capacity_wait` start; `Running{hold = capacity}` | header pause banner (`QuotaPause`), paused-time exclusion | S | Parent: the stopped turn. Attrs `provider`, `condition` (`quota_exhausted\|rate_limited\|unavailable`), `detail`, `resets_at`, `resumes_at`, `policy`, `fallback_provider`, `fallback_model`. The run holds only when `condition` is not `unavailable`. |
| `quota_waiting` | `run.agent_events` | span:`capacity_wait` update | banner "resumes at" | S | Attr `wait_resumes_at`. A later request replaces it. |
| `quota_wait_cancelled` | `run.agent_events` | span:`capacity_wait` update | banner | S | Clears `wait_resumes_at`; `reason` (`resumed\|paused`) is the next intent state. |
| `quota_resumed` | `run.agent_events` | span:`capacity_wait` end | quota stops (timeline exclusion) | S | Verdict `resumed`; attr `reason` (`operator\|wait_elapsed`). |
| `quota_abandoned` | `run.agent_events` | span:`capacity_wait` end | quota stops | S | Status `failed`, verdict `abandoned`, attr `reason`. |
| `provider_switched` | `run.agent_events` | span:`provider_switch` (instant) | header reason text | S | Attrs `from_provider`, `to_provider`, `to_model`, `reason`, `detail`; link `settles` to the `capacity_wait` span. |
| `rate_limit_update` | `run.agent_events` | metric series `rate_limit.<provider>.<window>` | none | S | Value is the used fraction. `limit`, `resets_at` and `window_minutes` are dropped until a screen needs them. |

### Evaluation, candidates and results

These are the strategy-shaped kinds. Today a commit observer derives most of them from core's
projection after each commit; in the new model the spans already carry the facts.

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `gate_started` | `run.evaluation`, `measurement_events` | span:`gate` start | candidate fold (gate spans), digest | S | The gate span exists. Add attrs `recipe`, `command`, `source`, `source_label`; `stage` already covers validation, build, accuracy and benchmark. |
| `gate_finished` | `run.evaluation`, `measurement_events`, read model | span:`gate` end; metric points | candidate fold, digest, input-gates tail, performance chart | M | Add `reused`. The readings (`metric`, `value`, `unit`) become metric points on the gate span (open question 4). `output_tail` is deleted: the gate stage's log stream is the tail. `round_label` is deleted. |
| `workstream_finished` | `run.integration` | span:`workstream` end attrs | none (`CoreState.workstreams` is unread) | S | **Strategy-specific.** Attr `failure`: kind (19 members of `WorkstreamFailureKind`, a dynamic-search vocabulary), role, turns, limit, text; plus `repeated_error`. `setup_failure` and `refunded` are always false today and are dropped. |
| `round_finished` | `run.integration`, read model | delete | digest "rounds finished", legacy-trace detection | S | **Strategy-specific** (serial rounds). The workstream span ends with the same facts: `attempts`, `judge_verdict` and `profile_skipped` become `workstream` attrs the serial searches fill; `perf_metric` and `perf_unit` are the gate's metric point. |
| `judge_result` | none (no producer) | delete | transcript row | S | Declared, never emitted. The judge's verdict is a `workstream` verdict. |
| `benchmark_result` | none (no producer) | delete | transcript row | S | Declared, never emitted; gate readings cover it. |
| `winner_selected` | `run.integration` | span:`adoption` attrs | none (`CoreState.winner` is unread) | S | `AdoptionAttrs.candidate` already names the revision. Add `selection` (`candidate\|baseline`) and `settlement_id`. |
| `input_measurement_failed` | `run.integration` | span:`gate` (subject `input`) attrs; notice | input gates panel, header diagnostic | M | Attrs `input_failure{kind, stage, attempts, compared}`. The redacted summary tail is a blob attachment. A notice makes it visible when no span is open. Add `subject` (`input\|candidate`) to `GateAttrs` instead of inferring input gates from time order. |
| `measurement_compared` | `run.integration` | span:`comparison` | idea figures (`comparisons`), idea standing | S | One span per comparison, updated when samples change. Attrs `metric`, `unit`, `direction`, `ratio`, `noise`, `verdict` (`better\|within_noise\|worse\|inconclusive`), `candidate_value`, `base_value`, `candidate_samples`, `base_samples`. Links `candidate` and `base` to the compared spans replace commit hashes as join keys. |
| `comparisons_read` | `run.host` | span:`baseline` | baselines in the hero | M | One span per baseline name (id `baseline:<name>`), updated in place when read again. Attrs `label`, `status`, `metric`, `unit`, `direction`, `value`, `spread`, `samples`, `hosts`, `reason`. Hidden from the timeline (open question 12). |
| `workspace_snapshot` | `run.git_events` | delete | transcript status row | S | `revision` and `parent_revision` are already `workstream` attrs. The changed files and diff are attached to the workstream at its end (blob `diff`, attr `files`); see the `query.design` row below. |
| `run_configured` | none (no producer) | delete | transcript status row | S | Declared, never emitted; `EpochStarted` is the configured run. |
| `framework_warning` | `run.git_events`, `gpu_events`, `host`, `integration` | notice | none shown (folded into diagnostics) | S | `Notice{severity, code, summary, detail, source, span}`. |

### Spans, projections and the operator

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `span_started` | `run.integration` | span | Run screen (trace, candidates, agents, timeline), digest, summary | M | `SpanRecord{change = started, span}`. The span is whole, replaced by id. |
| `span_updated` | `run.integration` | span | same | M | `change = updated`. |
| `span_ended` | `run.integration` | span | same | M | `change = ended`. |
| `experiments_changed` | `run.integration`, service | delete | none (`experimentsRevision` is unread) | S | A refetch hint for `query.experiments`; there is no query. |
| `iterations_changed` | `run.integration` | delete | browser session only | S | A refetch hint for `query.iterations`. The tree is the span parent links in the skeleton. |
| `control` | server controller | intent | none shown | S | Free text such as `/steer: ...` with a status becomes `IntentState`. The text forms (`/control refused: ...`, `/steer dropped: ...`) become typed steps and reasons. |
| `steer_delivery` | server integration | intent | run chat steer cards (`steerDeliveries`) | S | Steps `queued`, `reached`, `in_force`, `dropped`, `ended`, `refused`, with control id, action, role, attempt and reason. `reached` names the turn span. |
| `status_query` | server service, read model | delete | none | S | A viewer's read journaled as history. Viewers write nothing. |
| `chat` | chat manager | chat | run chat | M | An assistant message in the session log. The thread title is a session-log record. |
| `chat_thread_created` | chat manager | chat | run chat thread list | S | The session's first record: id, title, provider, model, created. Threads are discovered by listing `sessions/`. |
| `steer_proposed` | server integration | chat | run chat steer card | S | A record in the session log after the turn's `tool_result`. Nothing reaches the run until an operator intent is posted. |

The run-control transitions behind `control` and `steer_delivery` (the 17 `CoreEventType` members
from `steer_queued` to `action_applied`) are internal to the run process. They never reach a
viewer except as `IntentState` steps.

### Tally

Primary target of each of the 57 kinds:

| Target | Kinds |
|---|---|
| span (kind + attrs) | 20 |
| delete | 16 |
| lifecycle (one of them as a launch error when no run exists) | 7 |
| stream | 6 |
| chat-session log | 3 |
| intent state | 2 |
| epoch | 1 |
| metric series | 1 |
| notice | 1 |

Some kinds also use a secondary target, shown in the table: a blob attachment
(`agent_execution_started`, `agent_execution_finished`, `tool_result`), metric points
(`gate_finished`), a notice (`input_measurement_failed`) and a run hold in the lifecycle
(`quota_paused`). Of the 16 deletions, 3 have no producer today (`judge_result`,
`benchmark_result`, `run_configured`), 2 are legacy duplicates (`invocation_started`,
`invocation_finished`), 2 are refetch hints for queries that no longer exist
(`experiments_changed`, `iterations_changed`), and 1 journals a viewer's read (`status_query`).

### The envelope

| Field | Becomes |
|---|---|
| `sequence` | `Record.seq`, dense per `run.log` |
| `timestamp` | `Record.at`, wall time; spans keep their run-clock times |
| `run_id` | the run directory; no per-event copy |
| `text`, `status` | typed fields of the record; no free text |
| `round_label`, `agent_kind`, `execution_id`, `invocation_id` | deleted; a span id names the owner |
| `chat_thread_id` | the session directory |
| `span_id` | the owner field of the record (`StreamRef.span`, `MetricPoint.span`) |
| `diagnostic` | a `Notice` record |
| `truncated` | deleted; large content is a blob, kept whole |

### Server queries

Today's `query.*` requests (`src/server/api/protocol.py`) are folded into the record model or into
client folds. The desktop app sends `query.snapshot`, `query.iterations` and, to backfill history older than a
tail, `query.events`; the others are for the frozen web run page, which is dropped.

| Request | Response today | New home | Used by the desktop app |
|---|---|---|---|
| `query.snapshot` | `RunSnapshot`: status, active executions, chat threads, spans, overview | `Skeleton` (keyframe + tail): the lifecycle record, open `agent_turn` spans, `sessions/` listing, span table, `EpochStarted.run` | yes |
| `query.events` | an event interval, fenced by `store_id` | `Tail` from a sequence; there is no `store_id` because the log has one identity | yes (history paging) |
| `query.history` | persisted run history | the same `Tail` | no |
| `query.iterations` | a page of the policy's iteration tree | client fold over span `parent` links in the skeleton | yes |
| `query.performance` | rounds, objective context | metric series with unit and direction, `RunDescription.objectives`, the input's gate (`subject = input`); client fold | no |
| `query.experiments` | hypothesis log with revision deltas | client fold over `workstream` spans grouped by `hypothesis_id` (what `CandidateFold` does); search extras (claim, action, disposition) are `workstream` or `hypothesis` attrs | no |
| `query.design` | files each round touched | `workstream` attrs `files` and blob `diff`, attached at the workstream's end | no |
| `query.design_patch` | one file's patch | `GetBlobs` of the diff blob (per-file ranges in `files`) | no |

### Strategy-specific data

Places where today's data names one search, and what it becomes:

- `workstream` and `adoption` kinds are shared by every hypothesis search
  (`vibesys.search_spans`). Their attrs name candidates (`hypothesis_id`, `idea`, `revision`,
  `parent_revision`, `base_round`, `round`, `probe`). They stay strategy-declared span kinds.
  Core and the generic client need only: id, parent, links, status, verdict, and the metric points
  of the span.
- `round` and `base_round` exist for the serial searches. They are attrs of `workstream`, set only
  by searches that play rounds.
- `WorkstreamFailureKind` (19 members) is the dynamic search's vocabulary. It is the enum inside
  that search's `workstream` attrs, not a core type.
- `round_finished` and `workstream_finished` are the strategy-neutral commit observer's
  projections of core's round summaries. They become one span end each.
- `experiments_changed` and `iterations_changed` serve the hypothesis search's two read models.
  Both read models become folds over spans.
- `judge_verdict`, `profile_skipped` and `attempts` (round attrs) exist only where a judge or
  profile exists; they are optional `workstream` attrs.
- `gate`'s `stage` values (`build`, `accuracy`, `benchmark`, `validation`) belong to the evaluator
  contract, not to a strategy; they stay a gate attr.
- An evolutionary search declares its own kinds (a population, a generation, a mutation) the same
  way and needs nothing in core.

### The records

A sketch of `vibesys.store.v1`. Field numbers are placeholders. Validation rules
(protovalidate) are omitted for brevity and live in the same file.

```proto
syntax = "proto3";
package vibesys.store.v1;

message Record {
  uint64 seq = 1;                           // dense from 1 within run.log
  google.protobuf.Timestamp at = 2;         // wall time
  oneof body {
    EpochStarted epoch = 10;
    Lifecycle lifecycle = 11;
    SpanRecord span = 12;
    MetricPoint metric = 13;
    StreamRef stream = 14;
    BlobAttached blob = 15;
    IntentState intent = 16;
    Notice notice = 17;
    LegacyEvent legacy = 99;                // phase 1 only: {type, json}; deleted at cutover
  }
}

// ---- epoch ----
message EpochStarted {
  uint32 epoch = 1;                          // 1, 2, ... one per launch
  Checkout checkout = 2;
  RunDescription run = 3;
  repeated SpanKindDecl span_kinds = 4;
  repeated MetricSeriesDecl series = 5;
  google.protobuf.FileDescriptorSet descriptors = 6;  // attrs and reply messages
}
message Checkout { string tree_hash = 1; bool dirty = 2; string vibesys_version = 3; }
message RunDescription {
  string strategy = 1;                       // name of the registered search
  string input = 2;
  BlobRef objective = 3;                     // OBJECTIVE.md
  repeated Objective objectives = 4;         // headline first
  repeated string correctness_check = 5;     // argv
  optional Evaluator evaluator = 6;          // name, version; absent for a source tree
  optional uint32 max_workstreams = 7;
  repeated string expected_roles = 8;
}
message Objective { string series = 1; Direction direction = 2; }
message SpanKindDecl {
  string kind = 1;
  string attrs_type = 2;                     // full name in descriptors
  repeated string verdicts = 3;
  repeated string link_rels = 4;
  bool timeline = 5;                         // false: shown by dedicated views only
}
message MetricSeriesDecl {
  string name = 1;
  string unit = 2;
  Direction direction = 3;                   // MAX | MIN | NONE
  SeriesRole role = 4;                       // OBJECTIVE | RESOURCE | REFERENCE
}

// ---- lifecycle ----
message Lifecycle {
  oneof state {
    Starting starting = 1;
    Running running = 2;
    Draining draining = 3;
    Parked parked = 4;
    Ended ended = 5;
  }
}
message Starting {
  string stage = 1;                          // from the run environment's startup stages
  StageState state = 2;                      // RUNNING | WAITING | FINISHED
  optional Progress progress = 3;            // done, total, label
  optional string condition = 4;             // what a WAITING stage waits on
  optional double elapsed_seconds = 5;
}
message Running { optional Hold hold = 1; }
message Hold {
  oneof reason { OperatorHold operator = 1; string capacity_span = 2; }
}
message Draining { DrainGoal goal = 1; }     // PAUSE | PARK | STOP
message Parked {}
message Ended {
  Outcome outcome = 1;                       // FINISHED | FAILED | STOPPED | INTERRUPTED
  optional string reason = 2;                // core's EndReason
  string summary = 3;
  bool resumable = 4;
  bool uncompared = 5;
  optional RunFailure failure = 6;
}
message RunFailure {
  FailureKind kind = 1;     // BUDGET_EXHAUSTED | DEADLINE | NO_RESULT | ENVIRONMENT_UNAVAILABLE | CONFIGURATION
  string reason = 2;
  uint32 workstreams_started = 3;
  uint32 workstream_budget = 4;
  uint32 candidates_kept = 5;
  optional string adopted_candidate = 6;
}

// ---- spans ----
message SpanRecord {
  Change change = 1;                         // STARTED | UPDATED | ENDED
  Span span = 2;                             // whole, after the change; replace by id
}
message Span {
  string id = 1;                             // flow:<path>#<n> | op:<decision id>
  optional string parent = 2;
  RuntimeType runtime_type = 3;              // FLOW | OPERATION
  string name = 4;
  SpanStatus status = 5;                     // RUNNING | WAITING | PENDING | DONE | FAILED | STOPPED
  repeated WaitingOn waiting_on = 6;
  repeated Attempt attempts = 7;
  double started_at = 8;                     // run clock
  optional double ended_at = 9;
  optional Failure failure = 10;
  optional Stop stop = 11;
  optional Label label = 12;                 // set by the strategy
}
message Label {
  string kind = 1;
  string title = 2;
  optional string verdict = 3;
  optional string reason = 4;
  repeated SpanLink links = 5;               // {rel, to}
  google.protobuf.Any attrs = 6;             // the declared message of this kind
}

// ---- numbers, streams, content ----
message MetricPoint {
  string series = 1;
  optional string span = 2;
  double value = 3;
  double clock = 4;                          // run clock
}
message StreamRef {
  string id = 1;
  StreamKind kind = 2;                       // LOG | TRANSCRIPT
  optional string span = 3;                  // owner
  string title = 4;
  StreamState state = 5;                     // OPENED | ENDED | FAILED | DEGRADED
  optional uint64 length = 6;                // set when it closes
  optional BlobRef sealed = 7;               // a closed TRANSCRIPT, content-addressed
}
message BlobRef { string sha256 = 1; uint64 size = 2; string media_type = 3; }
message BlobAttached {
  string span = 1;
  string role = 2;                           // system_prompt | user_prompt | raw_result | diff | ...
  BlobRef blob = 3;
}

// ---- operator ----
message IntentState {
  string control_id = 1;
  IntentAction action = 2;                   // STEER | WITHDRAW | PAUSE | RESUME | DRAIN | STOP |
                                             // CANCEL_ATTEMPT | CANCEL_ITEM | SET_MAX_PARALLEL
  IntentStep step = 3;                       // APPLIED | REFUSED | REACHED | IN_FORCE | DROPPED | ENDED
  optional string reason_code = 4;
  optional string reason = 5;
  optional string span = 6;                  // the turn a steer reached
  optional SteerTarget target = 7;
  optional SteerMode mode = 8;
  optional string withdraws = 9;
}
message Notice {
  Severity severity = 1;                     // WARNING | ERROR | FATAL
  string code = 2;
  string summary = 3;
  optional string detail = 4;
  string source = 5;
  optional string span = 6;
}

// ---- strategy-declared attrs, as the shared hypothesis searches ----
message AgentTurnAttrs {
  string agent = 1; string role = 2; optional string model = 3;
  repeated string started_by = 4;
  string stage = 5; optional uint32 attempt = 6;
  optional string provider = 7; optional string reasoning_effort = 8;
  optional string harness_version = 9;
  optional TokenUsage usage = 10;            // set at the end
  google.protobuf.Any reply = 11;            // set at the end; descriptor in the epoch
}
message TokenUsage {                         // an absent field was not reported
  optional uint64 input = 1; optional uint64 cache_read = 2; optional uint64 cache_creation = 3;
  optional uint64 output = 4; optional uint64 reasoning = 5;
  optional double cost_usd = 6; optional uint64 duration_ms = 7;
}
message WorkstreamAttrs {
  optional string hypothesis_id = 1; optional string parent_revision = 2;
  optional uint32 base_round = 3; optional uint32 round = 4;
  optional string idea = 5; optional string revision = 6;
  optional string outcome = 7; optional string summary = 8;
  bool probe = 9;
  optional WorkstreamFailure failure = 10;   // dynamic search's FailureKind
  repeated FileChange files = 11;
  optional uint32 attempts = 12; optional bool profile_skipped = 13;
}
message GateAttrs {
  string stage = 1; GateSubject subject = 2; // INPUT | CANDIDATE
  optional string recipe = 3; optional string command = 4; bool reused = 5;
  optional FailedCheck failed = 6; optional InputFailure input_failure = 7;
}
message AdoptionAttrs {
  optional string candidate = 1; Selection selection = 2; optional string settlement_id = 3;
}
message ComparisonAttrs { /* metric, unit, direction, ratio, noise, verdict, values, samples */ }
message BaselineAttrs   { /* label, status, metric, unit, direction, value, spread, samples, hosts, reason */ }
message CapacityWaitAttrs { /* provider, condition, detail, resets_at, resumes_at, policy, fallback, ... */ }
```

Notes on the sketch:

- A span record carries the whole span, as today's span events do, so the client fold stays
  "replace by id" (open question 3).
- `MetricPoint` replaces the gate's inline reading rows: the performance chart is a series plot,
  and a search with no gates (an evolutionary one) emits points the same way (open question 4).
- Declared span kinds and series live in the epoch because a relaunch may change code. A client
  reads the latest epoch's declarations for new spans and the declaration current at a span's start
  for old ones.
- Unknown oneof members and unknown fields are skipped, which is what makes additive evolution
  safe in both directions.

## Migration

| Phase | Delivers |
|---|---|
| 1. Walking skeleton | protos, codegen and CI; Go gateway with `Hello` and `Tail`; Electron provisioning and one HTTP/2 connection behind a per-host switch; a Python converter that follows a live JSONL journal into `run.log` as `LegacyEvent{json}` records |
| 2. Complete read path | the inventory above in the store model; keyframes and `run.head`; `Skeleton`, `GetBlobs`, `TailStream`, `WatchRuns`; the new Run screen; conversion of all existing runs |
| 3. Write path | core state as generated types and `state.pb`; the run writes `run.log` natively; intents, drain and park, relaunch epochs; chat sessions; `HostCall` |
| 4. Cutover | delete the run protocol, its service and transports, the relay and run sockets, the helper protocol and its features, the reader server, registry tombstones and record versions, the dual summary fold, the conformance corpus, the strategy-specific event kinds, the Pydantic contract models and JSON Schema pipeline, the envelope JSON migration chain and the persisted-shape snapshots |

Existing JSONL runs, and `store.json` core envelopes, are converted to the new format. The
originals are kept for a week.

## Decisions and alternatives

| Question | Chosen | Alternatives and why not |
|---|---|---|
| Files or sockets between run and viewer | The run writes files; a gateway serves them | A per-run socket needs a relay, liveness handling and a bootstrap-then-tail protocol, and cannot show a parked or dead run. Files give one read path for live and ended runs and survive crashes. |
| Gateway language | Go, static binary | Python: needs an interpreter and environment on every host, slow start, and product logic leaks in. Rust: smaller binary, but cross-compiling from a Mac is more work and builds are slower. Go has first-party gRPC and HTTP/2 and builds static binaries for any host from the laptop. |
| Connection shape | One HTTP/2 connection per host | A channel per stream and role needs session limits and a relay. HTTP/2 multiplexes several concurrent streams and requests over one ssh channel. |
| Where the gateway comes from | The app ships it (cross-compiled, copied to the host) | The host serves the UI (version skew, a server to secure, browser needs ssh). A VS Code style remote server downloads a matching server per version; we do the same with a smaller artifact and no UI. |
| Contract language | `.proto` with protovalidate | JSON Schema generated from Pydantic: one direction of codegen, no streaming services, no descriptors for rendering unknown kinds, and the schema drifts from the code that checks it. |
| State and log | A (log records, then compare-and-swap `state.pb`, recovery drops past it) | B: `run.log` is core's own event-sourced journal. It is a bigger rewrite of recovery for a modest gain; kept possible. |
| RPC framework | gRPC | Connect: the benefit is browser and curl access, and the browser is dropped. Bidirectional streams (`Attach`) need HTTP/2 either way, and Node `http2` over our duplex is enough. Revisit if a browser client returns. |
| Browser support | Dropped | A browser cannot hold ssh. A future browser client is a local `vibesys ui` process that reuses the same bridge. |

## Open questions

1. **Committed versus visible.** A tailer can read a record the run appended before it
   compare-and-swapped `state.pb`. If the run dies in between, recovery drops that record, but a
   viewer already saw it. Proposed: the run publishes `committed_seq` in `run.head` after each
   swap, and the gateway serves only up to it. This costs a small write per commit and has to be
   settled before phase 3.
2. **A run that cannot write its own death.** `kill -9` and a host reboot leave no `ended` record.
   Proposed: the run holds an advisory lock on a file in its directory and the gateway probes it;
   the gateway reports `interrupted` for a log with no `ended` and no lock holder. The threshold
   and the stale-NFS case need measuring.
3. **Whole span or delta per span record.** Whole spans keep the fold trivial and match today's
   events; attrs are small once content moves to blobs. A delta would cost less on large updates.
4. **Readings as gate attrs or metric points.** Today the gate span carries `metrics` rows,
   `metric_value` and `vs_baseline`. Proposed: metric points are the single source and
   `vs_baseline` is a client fold against the input's gate. This changes `run.head`'s headline
   computation.
5. **Transcript form.** A running turn's transcript is a stream (live appends); the proposal seals
   it into a content-addressed blob when the turn ends. Two forms for one content, or only the
   stream?
6. **Notice is not in the agreed record list.** Warnings and failures that are for people
   (`framework_warning`, input failures, the old `diagnostic` envelope) need a home. Alternatives:
   lines in the narration stream, or a span status plus reason.
7. **Pause versus drain.** Pause holds new work and keeps the process (a capacity wait, a
   short hold); drain also exits. Both are modeled as holds with a goal. Should they be one intent
   with an `exit` flag?
8. **Steer delivery steps on `IntentState`.** The agreed outcome is `applied | refused`. Delivery
   (`reached`, `in_force`, `dropped`, `ended`) extends it. Alternative: delivery as links on the
   turn span.
9. **Reply models.** A turn's typed reply is a Pydantic model per role with a JSON Schema today.
   Rendering unknown replies by descriptor requires each reply model to become a proto message
   too. Alternative: `reply` as JSON text plus a schema id.
10. **Run clock across epochs.** Spans use the run clock, which deterministic replay depends on.
    Does it advance while a run is parked? Timeline durations across an epoch boundary need a
    rule.
11. **Hypothesis grouping.** Candidates are `workstream` spans that share `hypothesis_id`, folded
    by the client. An explicit `hypothesis` parent span would make the experiment log a span
    query, at the cost of a new kind.
12. **Timeline visibility of reference kinds.** `baseline` and `provider_switch` spans should not
    clutter the timeline. `SpanKindDecl.timeline` is a hint a strategy sets; unknown kinds default
    to shown.
13. **Dropped fields.** `usage_update` (context fill), `todo_update` entries and the rate-limit
    window details have no consumer. They are deleted or reduced here; say so now if a screen is
    planned.
14. **Pre-trace journals.** A journal with finished rounds and no span events predates the run
    trace; the digest reports its ideas as unknown. Convert them with no spans, or leave them
    unconverted?
15. **Inbox payload.** Requests are JSON files today with a reviewed allow-list of field names
    (`control_schema_fields`). They become protobuf bytes; the allow-list must move into the
    `.proto` validation.
16. **Record size.** The old per-event cap (`MAX_SERIALIZED_RUN_EVENT_BYTES`) existed for
    WebSocket frames. Pick a record cap for the log and decide what a producer does at the cap
    (always a blob, never truncation).
17. **Chat session discovery and retention.** Sessions are listed from the run directory. Whether a
    session outlives a relaunch, and how the default thread is named, are not decided.
