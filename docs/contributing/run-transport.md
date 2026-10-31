# Run transport

This page owns how a viewer (the desktop app) sees and steers runs on a host: what a run writes to
disk, the one gateway that serves those files, the one connection that carries them, and the event
model viewers read. It is written as the design. Parts not built yet are marked in the
[status table](#status). It replaces [`wire-protocol.md`](wire-protocol.md) and
[`desktop-connection.md`](desktop-connection.md), which describe the stack being retired.
How a run recovers from a stop is owned by [run-recovery.md](run-recovery.md), which also changes the
`state.pb` commit described below: the log is the authority and `state.pb` is a cache.

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

The existing transport remains the default. The gateway and record viewer are opt-in per host.
The migration phases are in [Migration](#migration); this table distinguishes the implemented
read path and journal projection from the native core and control work still to come.

| Part | Status | Phase |
|---|---|---|
| `vibesys.store.v1` and `vibesys.api.v1` protos, codegen, CI (`buf breaking`) | built; schema hashes derive from embedded descriptors | 1 |
| Go gateway `vibesysd`: `Hello`, `Tail` | built | 1 |
| Electron provisioning of the gateway; one HTTP/2 connection per host behind a per-host switch | built; uploads are verified before publication; cold bulk uploads can still fail on some SSH routes | 1 |
| Live JSONL journal projection into `run.log` | built as a tee installed before journal attachment; emits typed records | 1 |
| Store records for every event kind ([inventory](#event-inventory)) | schemas and typed journal projection built; the native core producer remains unbuilt | 2 |
| Skeleton keyframes, `run.head`, `~/.vibesys/runs/<run_id>` link | writers, discovery and converted head/keyframe publication built; legacy-only epochs have no native headline | 2 |
| `Skeleton`, `GetBlobs`, `TailStream`, `WatchRuns`; new Run screen | RPCs, client skeleton/cache and opt-in raw record panel built; complete native Run screen remains unbuilt | 2 |
| Conversion of existing JSONL runs and `store.json` envelopes | typed journal conversion and bulk project/host-index conversion built, including blobs, final keyframes, head publication and repair; journals that predate the run trace convert with the events they have, and unsupported journals are reported as skipped; core envelopes remain the resume authority until phase 3 | 2 |
| Core state as generated types, `state.pb`, native `run.log` writes | unbuilt; typed journal projection does not replace native core commits | 3 |
| Intents as records (`IntentState`), Pause/Pause now, relaunch epochs | inbox outcomes, Pause and Resume epochs built over the current saved-state resume path; native state recovery remains unbuilt | 3 |
| Chat sessions as processes with their own log; `HostCall` | built behind the per-host switch; reuses the existing auxiliary chat agent, prompts and read tools | 3 |
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
   subprocess of the host checkout. OS facts are not product logic: the gateway samples CPU, memory
   and GPU use in process, because a sample every 10 s cannot afford a Python start, and those
   numbers matter most when the checkout is missing or broken.

## What a run writes

One directory per run, opened only through `vs-project`'s `Project`.

| Path | What | Writer | Mutability |
|---|---|---|---|
| `run.log` | the history: ordered `vibesys.store.v1.Record`s | the run | append-only |
| `blobs/<sha256>` | large content: transcripts, diffs, tool output, prompts | the run | immutable, content-addressed |
| `streams/<id>` | log streams (build output, stage output, narration) | the run | append-only |
| `keyframes/<seq>.pb` | the skeleton at a log sequence | the run, at its checkpoints | immutable |
| `run.head` | tiny status and headline for dashboards, and `committed_seq` | the run | replaced atomically |
| `launch.lock` | serializes host relaunch admission until the new epoch is observed | the host command | held only during launch |
| `run.lock` | an advisory lock the live run holds; no content | the run | held while the process lives |
| `state.pb` | a cache of core's fold; records the log sequence it covers and the digest of those records | the run | replaced atomically (see [run-recovery.md](run-recovery.md#statepb-is-a-cache)) |
| `inbox/` | intents posted by viewers | the gateway | one file per request |
| `sessions/<id>/` | a chat session: its own `session.log` and inbox | the session process | as the run |

**Run discovery.** The run publishes `~/.vibesys/runs/<run_id>`, a symlink to its run directory,
written through `Project`. The gateway lists that directory and never rebuilds the project layout.

**`run.log` framing.** An 8-byte magic `VSRLOG01`, then per record: a uvarint payload length, a
4-byte little-endian CRC32C of the payload, and the payload (an encoded `Record`). A record is at
most 1 MiB. A producer puts content over 64 KiB in a blob; a writer asked to append more than the
cap raises, which is a bug in the producer and never truncation. A tail that ends inside a record
is a torn tail: the reader waits. A complete record whose CRC does not match is corruption: the
reader stops and reports it. A length over the cap is also corruption, which guards against a
damaged length varint.

**Commit and recovery.** One commit appends its log records, then compare-and-swaps `state.pb`,
which records the last log sequence it covers. On start, recovery drops log records past that
sequence, so the history never shows something core does not hold. After each swap the run writes
`committed_seq` in `run.head`, and the gateway's `Tail` and `Skeleton` serve only records up to it,
so a viewer never sees a record that recovery would drop. The phase 1 converter has no `state.pb`
and recovers every complete surviving record, syncing that prefix before publishing
it. Its writer groups ordinary appends: the first pending append wakes a dedicated
committer with a deadline 100 ms after the batch handoff, using the injected clock
and thread condition. Later appends do not reset that deadline. The committer
swaps out the pending batch under the append lock, then fsyncs the log, folds its
records, writes boundary and due keyframes and atomically publishes `run.head` outside
that lock. Ordinary appends never wait for fsync or projection publication.
Lifecycle changes, epoch headers and close request an immediate commit and wait
for durability.

The deadline does not depend on the owner event loop being free. Subject to
committer scheduling, an append handed off without a commit in flight becomes
durable within 100 ms plus its commit duration. An append handed off during a
commit becomes durable within the greater of 100 ms and the remaining in-flight
commit time, plus its own commit duration. Thus a storage stall longer than the
interval can contribute to two successive batches' lag; there is no fixed
completion bound without a storage-latency bound. Encoding and stream-write time
before the handoff are outside this bound.

`FileRunLogWriter` takes the run's clock and threads interfaces explicitly;
entrypoints supply the real implementations and tests supply simulated ones.
`FileRunLogWriter.append` retains its signature and acknowledges a write; ordinary
records await a later durability fence. `commit()` is the explicit durability
boundary, including for an epoch end not represented by a lifecycle record.
`RunLogTee.commit()` forwards that
boundary. The tee takes ownership of a configured `FileRunLogWriter`, so the
live journal can supply its run's clock and threads even when preparation
runs off-loop. Offline producers use the same committer. Close drains the final
suffix, checkpoints the epoch and joins the committer before releasing ownership.
Keyframes are written only
at commit time, at lifecycle/epoch boundaries or when the committed batch crosses
the record-count checkpoint threshold. Legacy-only logs still have no native
headline and retain their existing unfenced gateway compatibility path. The
`committed_seq` visibility fence applies once native declarations produce a head.
This log-only commit does not implement the `state.pb` transaction described above.
(Other orders were considered; see [Decisions](#decisions-and-alternatives).)

**Blobs and streams.** A record never carries large content inline. It carries a `BlobRef` (hash,
size, media type) and the client fetches by hash, caching in IndexedDB forever because the content
cannot change. Log streams are append-only files read by offset. A turn's transcript is a stream
only; when the turn span ends, its attrs record the stream's final length, which makes (stream id,
length) immutable and cacheable. There is no second, sealed copy.

**Skeleton keyframes.** The skeleton is what a client holds in memory to scrub: the span table
with each span's versions by sequence (status, verdict, attrs), the metric series, the stream
index and the epoch list. The run writes a keyframe at its checkpoints (a bounded number of
records, a lifecycle change, an epoch end). The `Skeleton` call returns the latest keyframe plus
the log records after it, so opening a run reads a keyframe, not the log. A run that ends or parks
ends on a keyframe, so its reply has no records after it. On the wire the keyframe names each
epoch's schema descriptors by hash and carries a descriptor set only when the reader has not
listed it, since every epoch of a run repeats one set.

**`run.head`.** The headline a list row draws: lifecycle, strategy name, objective and best result
against the input, counts of work units, token and cost totals, a bounded point list for the tile
chart, and whether the run needs its user. The run writes it, computing the headline from the same
metric points a client plots; no reader folds a journal for it.
This deletes the second (Python) copy of the summary fold.

The canonical epoch comes from committed `EpochStarted` records. Process metadata
never supplies it. For retained phase-1 heads whose epoch cache is zero, `WatchRuns`
decodes the published log prefix into its response without rewriting either file.
An explicit zero committed sequence admits no records; an absent sequence retains
the phase-1 complete-prefix rule. Without a log the epoch remains unknown. Native
heads with a populated epoch cache avoid this scan. Python observation readers use
`read_run_head`, which decodes missing caches through the same committed-prefix fold
as `read_current_epoch`; clients consume the canonical response field directly. Lock probes still decide liveness independently.

## Intents and epochs

**Operator controls** are Pause, Pause now, Resume and Guidance. A viewer posts a typed intent
through `HostCall` to the existing ordered inbox. Admission acknowledges persistence; the run's
`IntentState` records application or refusal. Request IDs retain their disposition across retries.
Scoped guidance and work-item actions continue to use the same inbox.

**Pause** closes admission, drains every in-flight logical turn and its evaluation requests
within the turn's original deadline, releases this run's evaluation interests, and exits.
Independent queued evaluations do not start. **Pause now** cancels unfinished work and exits,
including as escalation during a drain. It warns: "Unfinished work may need repeating and may
still incur charges." Neither interruption becomes a policy failure or retry debit. Actual
usage and accepted results remain saved. The quota hold is internal.

A pause never exits with a request in flight. Both pauses close admission and wait until every
request already dispatched has recorded its end. A pause may carry a drain deadline
(`drain_seconds`; the run's cleanup bound for Pause now). At the deadline, a request that can be
sent again goes back to unsent and is dispatched again by the next epoch, and the rest is
cancelled and ends at its limit a cleanup bound later. The drain deadline is saved in the run's
park (`RunPark.deadline_at`), so a restart keeps it.

**Resume** permits every prior ending and starts a new epoch with a fresh duration, defaulting
to the original length. Prior closing intents cannot close the successor epoch. It leaves the
operator's project checkout in place. P1 uses today's core store and loader; retained older
formats that lack a loadable core envelope need the later facts importer. Their content stays
retained, and the diagnostic names that prerequisite rather than refusing an ending kind.

**Relaunch** starts the same run as a new epoch from the host checkout. The `EpochStarted` record
records the tree hash and a dirty flag, so the timeline shows each epoch boundary and which code
ran in it. A run restarts from `state.pb`; its open spans from the previous epoch are resumed or
ended by the same rules a restart after a code change uses today
([flows.md](flows.md#the-run-trace)). A live run holds a `flock` on `run.lock`, and the gateway
probes it without blocking. The run and the gateway share a host (principle 2), so only local lock
semantics matter. A relaunch first writes the missing `Ended(INTERRUPTED)` for the previous epoch,
then its `EpochStarted`, so the log holds the fact and the gateway's inference (a log with no
`Ended` and no lock holder) is only a live hint. The run clock is monotone across epochs and
frozen while parked; it resumes from `state.pb`. `Record.at` carries wall time. The timeline marks
epoch boundaries with wall-time labels and collapses parked gaps.

The current relaunch implementation uses the existing saved core envelope until the generated
state migration supplies `state.pb`. The plain `vibesys.api.relaunch` function and
`vibesys relaunch --resume <run-reference>` share that resume path. The journal projection writes
one outcome per inbox request before its acknowledgement, retaining the legacy frontend's
control events. Relaunch restores missing inbox markers from recorded decisions before judging
pending requests again. New records carry `Record.clock`, excluding downtime; older records without it
derive elapsed time within each epoch from their wall timestamps. Span times retain the epoch
in which each transition occurred, including spans that remain open across a relaunch.

The launch operation for an existing run is the typed HostCall relaunch method, not a second
Launch RPC. Both would dispatch the same host operation; HostCall keeps checkout selection and
bounded subprocess ownership in one place, with no run policy in the gateway. The host command
uses the existing detached launcher; its server resumes through `vibesys.api.relaunch`. The
command returns the epoch actually observed in `run.log`, or a typed refusal: locked, ended,
checkout missing, or epoch changed. An unexpected launch failure or an unconfirmed epoch uses
HostCall's configuration diagnostic. It is uncertain whether a timed-out call launched, so the
client never retries it automatically. Requests name the observed epoch; a retry against a later
epoch is refused. The host command holds a separate launch lock across admission and epoch
confirmation, so concurrent app calls cannot both admit the same observed epoch. The writer
still acquires the lifetime lock atomically.

The app currently validates and selects a host checkout but offers no safe update operation.
The operator updates that checkout before relaunching. No update-checkout method is exposed.
The new epoch records the checkout's tree hash and dirty flag, including operator edits.

**Chat sessions** belong to the run and outlive relaunches as data. They are separate processes
with their own `session.log` and inbox, started on demand and exiting when idle or when paused. The
default session id is `default`, and sessions are kept as long as the run is. They are not the
run: a session reads the run's files, its answers and token streaming are appends to its own log,
and a steer it proposes is data in that log that the operator may send as an intent. A generic
`Attach` bidirectional byte stream to a socket a session owns is reserved for future byte-level
interaction (terminals). The run never serves one.

`vibesys host chat-message` admits a typed message to the session inbox and starts its process
if needed. Admission and idle exit share a lock, so a message cannot be stranded by an idle
exit. The session uses the shared runlog writer and publishes its own commit fence in
`sessions/<id>/run.head`. `Skeleton.sessions` lists the conversations; `Tail` and `GetBlobs`
select their files with `session_id`. The desktop mounts chat beside the Run screen and sends
an operator-approved proposal through the same run-intents feature as the run controls.

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
| `Subscribe` | stream | one topic's keyed state, then conflated changes; see below |
| `PostIntent` | unary | writes the run's (or session's) inbox |
| `HostCall` | unary | typed host operations, including `relaunch` of an existing run; `vibesys host <method>` with protobuf on stdin and stdout |
| `Attach` | bidi | reserved |

The gateway preserves framed record bodies. Its store read boundary supplies semantic upgrades
for retained records and keyframes before serving them.

`Subscribe` is the one form for live state where a reader who missed five updates only wants the
sixth. A topic is a map of key to latest value. The gateway runs one producer per topic instance
and stops it when the last subscriber leaves, so a blocked client costs key names, never a queue.
A topic's cadence, staleness and bounds are its `TopicPolicy` in `subscribe.proto`, and a failed
frame ends the stream with a typed code. Anything that needs every value, such as a log, stays on
`Tail`. `internal/subscription` owns the mechanism and `subscriptiontest` the contract each topic
passes. Server keepalive ends a half-open connection, and with it the streams that hold a producer.

The frame timing is stated once, as `SubscribeRequest`'s `subscribe_timing` option: the hub heartbeats
at `heartbeat_ms`, and a client counts a stream with no frame for `lease_ms` as lost. The generator
projects both into Go and TypeScript tables and refuses a lease that cannot hold a beat. The client
sends no keepalive of its own: the gateway server sets no keepalive enforcement policy, so a client
ping faster than gRPC's default would be refused, and the lease on frames already finds a silent
stream. A client reads a stream through `@vibesys/subscription`, in main through `Subscriptions`,
and never through the generic gateway port, which refuses `Subscribe`. A lost session ends a
`Subscribe` stream instead of re-issuing it, because a second stream in one call would deliver a
second snapshot into state the caller already holds; the subscription's machine opens the next one.

## Versions and upgrades

Two version domains, with different rules.

**Wire (`vibesys.api.v1`).** `Hello` requires an exact API-schema-hash match. There is no
compatibility and no feature flag. Because the app ships the gateway from its own tree, the two
match by construction; a mismatch means a stale copy on the host, and the app replaces it.

**Store (`vibesys.store.v1` and core state).** Additive changes only, enforced by `buf breaking`
in CI. New code must read data written by the last 7 days of nightlies; a CI test resumes saved
runs from each. The retained-writer corpus is checked through Python head reads, gateway
Skeleton/Tail/WatchRuns, and the client screen projections. A semantic break bumps the package
version and ships one upgrade function. A newer writer is refused by name, never read as if it
were older.

The store read boundary restores the input measurement of pre-field runs from their recorded
input gate or spanless objective point. It adds the typed fact to versioned declarations; a
record can carry the amendment alongside its original body. The fact takes effect at the
measurement's sequence, including during historical seeking. Explicit measured or unmeasured
facts take precedence. Heads get their missing input value from this same upgrade; retained
files are unchanged and readers require no writer restart.

Run discovery recovers an older index entry's missing project address from the committed epoch
headers through the same bounded observation memo. No address backfill or writer restart is
required. If no project can be recovered, the listing and dashboard retain the run with an
unknown project and expose no project control address.

The same observation boundary restores unstated performance and terminal presentation from
previous-writer committed prefixes. Head, keyframe and Skeleton readers share the projection;
the card and page consume those facts. Keyframes and Skeleton replies carry the exact-prefix
read head, while stored files remain unchanged. Explicit writer-stated facts win. The Go
retained reader is checked against the Python core's generated performance histories.

**Development mode.** The laptop tree builds the UI and the gateway; the host checkout runs the
runs. They may diverge as long as store changes stay additive. The operator updates the host
checkout, including any intended uncommitted edits, before relaunching. The app does not
currently sync or pull it.

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
- **Notice** carries a human-facing warning or failure: a severity, a code, a message and an
  optional span id. Narration lines are unstructured and a span status cannot hold run-level
  warnings, so neither serves.

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
| `run_started` | `vibesys.api` session, server | epoch | live page fold (objective, metric), digest, run map | M | Strategy name, input, expected roles and `RunOverview` go in `EpochStarted.run` (`RunDescription`); search limits go in its descriptor-backed `SearchRunDescription` detail. Objective text is a blob. Unit and direction are in the series declarations, so the server no longer back-fills them into a snapshot. |
| `run_startup_progress` | `vibesys.run.resources` | lifecycle | startup bar (`run-session`), startup timing words (`scoped-logs`) | S | `Lifecycle.starting{stage, state, condition, elapsed, progress}`. One record per step; bounded by the number of image-build steps. |
| `workspace_root_probe` | `slurm_probe_events` | span:`environment_probe` | none (ignored by the fold) | S | Runtime-declared kind. Attrs: `state`, `job_id`, `pending_reason`, `waited_seconds`, `queue_timeout_seconds`, `failure`, `attempt`, `retry_in_seconds`. A `failed` probe ends the run with `RunFailure.kind = environment_unavailable`. |
| `run_status_changed` | server controller, journal | lifecycle | `CoreState.status`: header, recent-status words | S | `starting\|running\|pausing\|paused\|stopping\|stopped\|completed\|failed` becomes `Running{hold}`, `Draining{goal}`, `Parked`, `Ended{outcome}`. `previous` is derived. |
| `run_finished` | controller, runtime, session | lifecycle | fold terminates run; digest closing | S | `Ended{outcome = finished}`. |
| `run_failed` | controller, runtime, session | lifecycle | fold terminates run; digest closing; failure text | S | `Ended{outcome = failed, failure = RunFailure}` carries generic kind and reason. Its descriptor-backed `SearchRunFailure` detail carries workstreams started and budget, candidates kept, and adopted candidate. |
| `run_interrupted` | controller, runtime | lifecycle | fold terminates run | S | `Ended{outcome = interrupted, interruption_reason, signal}` when the process caught the signal. A process that cannot write its own death leaves a log with no `Ended` and no lock holder; the gateway reports `interrupted` as a hint and the next relaunch writes the record (see Intents and epochs). |
| `run_ending` | `vibesys.run.core_run` | lifecycle | host summary (`with_ending`): end reason, resumable | S | Merged into `Ended`: `summary` and `resumable`, plus the descriptor-backed `SearchEnded` detail with its closed search `reason` and `uncompared`. `rounds_finished` is dropped (count ended workstream spans). |
| `configuration_failed` | server runtime, read model | lifecycle, or launch error | transcript line; diagnostics | S | Raised before a run exists: it is the `Launch` or `HostCall` error. If a run directory exists it is `Ended{failed, kind = configuration, code, stage, message, exit_code}`. |

### Agent turns and their content

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `invocation_started` | server execution | delete | none | L | Legacy duplicate of `agent_execution_started`; the journal already rewrites it into that kind. |
| `invocation_finished` | server execution | delete | none | L | Same, for `agent_execution_finished`. |
| `agent_execution_started` | server execution, `run.integration` | span:`agent_turn` attrs, blob | agents view, step narrative, usage join (`ExecutionStart`) | L | The span already exists. Add attrs `stage`, `attempt`, `provider`, `model`, `reasoning_effort`, `harness_version`. `system_prompt` and `user_prompt` are blob attachments (`system_prompt`, `user_prompt`). The execution id is the span id, so the usage join disappears. |
| `agent_execution_activity_changed` | server execution | delete | active-turn word (`activeExecutions`) | S | Derived by the client from the tail of the turn's transcript stream (last entry kind and tool). A past sequence needs no activity. |
| `agent_execution_finished` | server execution, `run.integration` | span:`agent_turn` end attrs, blob | turn rows (typed reply fields), idea outcomes | L | `reply` (kind, value) is JSON text in one attr until phase 3, when reply models become proto messages and the field is reserved and deleted after the 7-day read window. `result` (the raw text) is a blob attachment (`raw_result`). `error` is the span's failure. |
| `agent_output_chunk` | server execution, `run.agent_events` | stream (`transcript`) | step narrative, turn rows | M | Transcript entries `assistant`, `analysis`, `prompt`, `diagnostic`. The channel `tool` is dropped (typed tool entries replace it). `status` (progress, label, elapsed, tokens) is presentation and is deleted. |
| `tool_call` | `run.agent_events` | stream (`transcript`) | turn rows (tool cards) | M | Entry: tool, call id, args. Args over the inline cap go to a blob. |
| `tool_result` | `run.agent_events` | stream (`transcript`), blob | turn rows | L | Entry: tool, call id, error flag, summary. `content` and `payload` (command stdout and stderr, JSON) are a blob when over the cap. |
| `todo_update` | `run.agent_events` | delete | none | S | No consumer. Re-adding an entry kind later is additive. |
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
| `rate_limit_update` | `run.agent_events` | metric series `rate_limit.<provider>.<window>` | none | S | Value is the used fraction. `limit`, `resets_at` and `window_minutes` are dropped (no consumer; re-adding is additive). |

### Evaluation, candidates and results

These are the strategy-shaped kinds. Today a commit observer derives most of them from core's
projection after each commit; in the new model the spans already carry the facts.

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `gate_started` | `run.evaluation`, `measurement_events` | span:`gate` start | candidate fold (gate spans), digest | S | The gate span exists. Add attrs `recipe`, `command`, `source`, `source_label`; `stage` already covers validation, build, accuracy and benchmark. |
| `gate_finished` | `run.evaluation`, `measurement_events`, read model | span:`gate` end; metric points | candidate fold, digest, input-gates tail, performance chart | M | Add `reused`. The readings (`metric`, `value`, `unit`, `vs_baseline`) are not attrs: metric points on the gate span are the single source, and `vs_baseline` is a client fold. `output_tail` is deleted: the gate stage's log stream is the tail. `round_label` is deleted. |
| `workstream_finished` | `run.integration` | span:`workstream` end attrs | none (`CoreState.workstreams` is unread) | S | **Strategy-specific.** Attr `failure`: kind (19 members of `WorkstreamFailureKind`, a dynamic-search vocabulary), role, turns, limit, text; plus `repeated_error`. `setup_failure` and `refunded` are always false today and are dropped. |
| `round_finished` | `run.integration`, read model | delete | digest "rounds finished", legacy-trace detection | S | **Strategy-specific** (serial rounds). The workstream span ends with the same facts: `attempts`, `judge_verdict` and `profile_skipped` become `workstream` attrs the serial searches fill; `perf_metric` and `perf_unit` are the gate's metric point. |
| `judge_result` | none (no producer) | delete | transcript row | S | Declared, never emitted. The judge's verdict is a `workstream` verdict. |
| `benchmark_result` | none (no producer) | delete | transcript row | S | Declared, never emitted; gate readings cover it. |
| `winner_selected` | `run.integration` | span:`adoption` attrs | none (`CoreState.winner` is unread) | S | `AdoptionAttrs.candidate` already names the revision. Add `selection` (`candidate\|baseline`) and `settlement_id`. |
| `input_measurement_failed` | `run.integration` | span:`gate` (subject `input`) attrs; notice | input gates panel, header diagnostic | M | Attrs `input_failure{kind, stage, attempts, compared}`. The redacted summary tail is a blob attachment. A notice makes it visible when no span is open. Add `subject` (`input\|candidate`) to `GateAttrs` instead of inferring input gates from time order. |
| `measurement_compared` | `run.integration` | span:`comparison` | idea figures (`comparisons`), idea standing | S | One span per comparison, updated when samples change. Attrs `metric`, `unit`, `direction`, `ratio`, `noise`, `verdict` (`better\|within_noise\|worse\|inconclusive`), `candidate_value`, `base_value`, `candidate_samples`, `base_samples`. Links `candidate` and `base` to the compared spans replace commit hashes as join keys. |
| `comparisons_read` | `run.host` | span:`baseline` | baselines in the hero | M | One span per baseline name (id `baseline:<name>`), updated in place when read again. Attrs `label`, `status`, `metric`, `unit`, `direction`, `value`, `spread`, `samples`, `hosts`, `reason`. Hidden from the timeline (`SpanKindDecl.timeline` is a hint; unknown kinds are shown). |
| `workspace_snapshot` | `run.git_events` | delete | transcript status row | S | `revision` and `parent_revision` are already `workstream` attrs. The changed files and diff are attached to the workstream at its end (blob `diff`, attr `files`); see the `query.design` row below. |
| `run_configured` | none (no producer) | delete | transcript status row | S | Declared, never emitted; `EpochStarted` is the configured run. |
| `framework_warning` | `run.git_events`, `gpu_events`, `host`, `integration` | notice | none shown (folded into diagnostics) | S | `Notice{severity, code, message, span}`. |

### Spans, projections and the operator

| Kind | Producer | Target | Viewer today | Size | Notes |
|---|---|---|---|---|---|
| `span_started` | `run.integration` | span | Run screen (trace, candidates, agents, timeline), digest, summary | M | `SpanRecord{change = started, span}`. The span is whole, replaced by id. |
| `span_updated` | `run.integration` | span | same | M | `change = updated`. |
| `span_ended` | `run.integration` | span | same | M | `change = ended`. |
| `experiments_changed` | `run.integration`, service | delete | none (`experimentsRevision` is unread) | S | A refetch hint for `query.experiments`; there is no query. |
| `iterations_changed` | `run.integration` | delete | browser session only | S | A refetch hint for `query.iterations`. The tree is the span parent links in the skeleton. |
| `control` | server controller | intent | none shown | S | Free text such as `/steer: ...` with a status becomes `IntentState`. The text forms (`/control refused: ...`, `/steer dropped: ...`) become typed steps and reasons. |
| `steer_delivery` | server integration | span link | run chat steer cards (`steerDeliveries`) | S | The turn span that received a steer links to the request id; `refused` is the `IntentState`. The client derives `queued`, `reached`, `in_force`, `dropped` and `ended` from the links and the span's status. |
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
| delete | 17 |
| lifecycle (one of them as a launch error when no run exists) | 7 |
| stream | 5 |
| chat-session log | 3 |
| intent state | 1 |
| span link (steer delivery) | 1 |
| epoch | 1 |
| metric series | 1 |
| notice | 1 |

Some kinds also use a secondary target, shown in the table: a blob attachment
(`agent_execution_started`, `agent_execution_finished`, `tool_result`), metric points
(`gate_finished`), a notice (`input_measurement_failed`) and a run hold in the lifecycle
(`quota_paused`). Of the 17 deletions, 3 have no producer today (`judge_result`,
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
| `query.experiments` | hypothesis log with revision deltas | client fold over `workstream` spans grouped by `hypothesis_id` (what `CandidateFold` does); search extras (claim, action, disposition) are `workstream` attrs | no |
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

The authoritative records and their validation rules live in
[`proto/vibesys/store/v1/record.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/store/v1/record.proto)
and the concern-specific files it imports. Typed attrs are defined in
[`agent_attrs.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/store/v1/agent_attrs.proto),
[`search/v1/attrs.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/search/v1/attrs.proto), and
[`environment_attrs.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/store/v1/environment_attrs.proto).
Search run configuration, settlement and failure details are defined in
[`search/v1/run.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/search/v1/run.proto).
The store package never imports the search package. Strategy details travel as
`Any` with their descriptors in `EpochStarted.descriptors`, just like span attrs;
`test_store_schema_cannot_import_search_strategy_contracts` enforces the dependency boundary.

These files replace the illustrative proto sketch; their published field numbers are authoritative.

`EpochStarted` retains its original checkout and schema identity fields and adds the run description,
kind declarations, series declarations, descriptors and version. `LegacyEvent` remains readable for
seven days after phase 2 lands. Existing phase 1 fields retain their validation behavior.

Inline text fields are bounded at 64 KiB by protovalidate. Content above that cap is
attached as a blob, including an oversized interim JSON reply under role `reply`. The serialized
1 MiB record cap is enforced by the log writer and reader, which can measure the actual bytes;
protovalidate cannot measure encoded protobuf size. Declaration resolution and validation of an
`Any` payload against its declared attrs type require the epoch descriptors at the read boundary.

The journal bridge amends `EpochStarted` as it discovers declarations: consumers replace
an epoch by its number, and each amendment retains the earlier declarations. Historical
journals without a checkout fingerprint explicitly set `provenance_known` to false.
They do not invent a checkout identity.

Transcript streams contain unsigned-varint-length-prefixed `TranscriptEntry` messages,
defined in
[`transcript.proto`](https://github.com/uw-syfi/vibesys/blob/main/proto/vibesys/store/v1/transcript.proto).
Each text, tool-call or tool-result entry holds inline content or a `BlobRef`. The turn's
whole-span replacement seals the stream's final byte length when the turn ends.
Every entry carries `at`, the time of the event it records; entries written before the field
existed have none, and a reader shows no time for them. A tool result holds the text the agent
was shown (`content`) and, when the tool reported more, a typed `payload`: a command's stdout and
stderr (each inline or a blob), its exit code and its run time, or a JSON value. A reader that
does not know a payload case shows `content` alone. The earlier `payload_json` field is
deprecated: it is no longer written, and a reader ignores a record's copy and shows `content`.
For older string attrs without a blob alternative, the bridge writes `blob:<sha256>`
and a `BlobAttached` whose role names the field path; an oversized interim reply instead
omits its inline JSON and uses the `reply` attachment.

Built-in strategy attrs always use their declared search message. A historical custom
span kind with no known schema declares `google.protobuf.StringValue` and preserves its
exact JSON text. Historical strategy facts without a reliable span owner become
`Notice.UNATTRIBUTED_EVENT` with typed `detail`; old gate stages without an owner use
`GateSubject.UNATTRIBUTED`. Neither path invents a workstream or an input-gate identity.

Notes on the records:


- A span record carries the whole span, as today's span events do, so the client fold stays
  "replace by id". Deltas are not used; revisit only if measured record bytes say so.
- `MetricPoint` replaces the gate's inline reading rows: the performance chart is a series plot,
  and a search with no gates (an evolutionary one) emits points the same way. The run computes `run.head`'s headline from the same points.
- Declared span kinds and series live in the epoch because a relaunch may change code. A client
  reads the latest epoch's declarations for new spans and the declaration current at a span's start
  for old ones.
- Unknown oneof members and unknown fields are skipped, which is what makes additive evolution
  safe in both directions.
- No `hypothesis` span kind: the client groups `workstream` spans by `hypothesis_id`, and a
  strategy may declare its own kind later.
- `reply_json` is the phase 1 form of a turn's reply. In phase 3 each reply model becomes a proto
  message with the core types, `reply_json` is reserved, and its field is deleted after the 7-day
  read window.
- Pre-trace journals (finished rounds, no span events) are not converted; the converter reports
  each as skipped.

## Migration

| Phase | Delivers |
|---|---|
| 1. Walking skeleton | protos, codegen and CI; Go gateway with `Hello` and `Tail`; Electron provisioning and one HTTP/2 connection behind a per-host switch; a Python converter that follows a live JSONL journal into `run.log` as `LegacyEvent{json}` records |
| 2. Complete read path | the inventory above in the store model; keyframes and `run.head`; `Skeleton`, `GetBlobs`, `TailStream`, `WatchRuns`; the new Run screen; conversion of all existing runs |
| 3. Write path | core state as generated types and `state.pb`; the run writes `run.log` natively; intents, Pause and Pause now, Resume epochs; chat sessions; `HostCall` |
| 4. Cutover | delete the run protocol, its service and transports, the relay and run sockets, the helper protocol and its features, the reader server, registry tombstones and record versions, the dual summary fold, the conformance corpus, the strategy-specific event kinds, the Pydantic contract models and JSON Schema pipeline, the envelope JSON migration chain and the persisted-shape snapshots |

Existing JSONL runs are converted to the typed store read model in phase 2. The converter
publishes the log, its final skeleton keyframe and, when the journal declares a run and
lifecycle, its head through the run-log writer's durable publication mechanism. A previous
conversion missing these derived files is repaired without rewriting its complete log.
Bulk conversion covers the selected project's saved runs and every entry in the host run
index, reports each outcome and reason, and continues after individual failures. A run
without trace events is explicitly skipped (OQ14); unsupported historical event kinds and
index entries are reported rather than silently ignored. Runs must be stopped first.

`store.json` has a storage envelope around an opaque runtime record, whose `envelope` holds
core and strategy state. The core JSON migration chain remains authoritative for resume
until phase 3 generates core state types and writes `state.pb` with its log commit fence.
Phase 2 converts the journal projection alongside these envelopes without changing their
bytes or manufacturing a `state.pb` sequence. A store-only run has no phase 2 read history
and is explicitly reported as unsupported, with core conversion deferred to phase 3.
The golden envelope conversion tests preserve saved stores and check their existing core
migration chain; the generated-journal properties and gateway conversion fixture check the
read projection and keyframe opening.

Original journals and core envelopes are retained for at least seven days. Conversion
never deletes them; retention cleanup is separate from conversion.

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
| Order of log append and `state.pb` swap | Log first, then swap, with `committed_seq` bounding what is served | Writing `state.pb` before the log: state would name records not yet on disk. |
| Transcript form | One stream; the turn span's end records its final length | A sealed content-addressed blob copy when the turn ends: two forms for one fact. |
| Run controls | Pause drains and exits; Pause now cancels and exits; Resume opens a new epoch; Guidance persists advice | A separate permanent ending control made useful completed runs impossible to continue. |
| Turn reply typing | Proto messages in phase 3; JSON text in one reserved field until then | Descriptor-rendered `Any` from phase 1 would force every reply model into proto before the core types move. |
| Run-level warnings | A `Notice` record | Lines in the narration stream are unstructured; a span status cannot hold a warning with no span. |
| Browser support | Dropped | A browser cannot hold ssh. A future browser client is a local `vibesys ui` process that reuses the same bridge. |

## Open questions

- **Lock behaviour on an NFS root.** The `run.lock` design assumes local `flock` semantics, which
  hold because the run and gateway share a host. Whether a lock holder is still visible when the
  run directory is on an NFS root is measured in phase 2.
