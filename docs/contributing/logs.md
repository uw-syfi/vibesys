# Log streams

The output of long-running work in a run (image builds, container starts, evaluator
stages, tool builds, Slurm jobs, the run's own narration) is kept as log streams, so a
person can watch it live in the desktop app and read it after the run ends.

Logs are for humans only. Facts go through typed events and records; product code never
reads a log back, and never parses its own processes' output for facts. Parsing a foreign
tool's output at its call site, through a `tee` of its sink, is allowed.

## Layers

| Layer | Owner | Knows |
| --- | --- | --- |
| Producers | process runners, `vs_sandbox`, `vs_slurm`, `vibesys` | Write bytes to a `vs_sim.api.OutputSink`; nothing else. |
| Recorder | `vs_logs.api` | Streams, offsets, the cap, the index. No docker, gates or UI. |
| Owners | `vibesys.log_owners` (`LogOwner`) | What each stream is for, as a typed reference to the run model; `span_id` names the run-trace span it belongs to when its writer knows it: a gate stage's stream names its gate's operation span (`op:<decision>`), from the decision its measurement answers (`MeasurementDecisions`, which `MeasurementRequests` fills as it submits; a job an earlier host submitted names none). |
| Reader | `vs_logs.read` | List streams; read a bounded chunk from an offset or the tail. |
| Serving | `server.host_helper_logs` (`logs` feature), over `server.host_helper_os.StoreLogReader` | Chunk caps, long-poll follow, discovery. |
| Clients | `clients/` logs lib | Selection, follow, rendering. |

## Writing

A producer is handed a `LogScope[LogOwner]` and opens one stream per command:

```python
with scope.open(StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE), "docker build .") as sink:
    processes.run(command, output=sink)
```

The stream is an `OutputSink`; leaving the block closes it `ended`, or `failed` when an
exception leaves it.

A log failure never hurts the work being logged. Writing to a stream and closing it never
raise, as the `OutputSink` contract says, so a drain thread never dies and a producer never
fails on a full disk:

- A write the store cannot complete (an `OSError`, such as a short write to a full disk)
  degrades the stream: its later bytes are dropped, the store says so once through the
  `warn` it was built with, and the stream's close record says `degraded`.
- A write after close is dropped silently.
- An index record the store cannot append is dropped after the store cuts the torn line it
  left, so the next record starts a line. A stream whose `opened` record was dropped takes
  writes and keeps nothing. The index's first failure is warned once.
- Opening a store cuts a torn last index line before it appends, so a short write never
  corrupts the index for later opens. A reader ignores a torn last line of the index or of
  a stream's pipe marks.

A library never sees a scope or an owner. It takes an opener in its own vocabulary, a
factory that returns a context manager of an `OutputSink`, calls it once per command or
stage, and passes the entered sink down per call to the runner that writes it:

| Opener | Called with | Carried by |
| --- | --- | --- |
| `vs_runtime` `StartupOutputOpener` | startup stage, command description | `RunEnvironmentRequest.startup_output` |
| `vs_runtime` `StageOutputOpener` | `StageRun(handle_id, stage_id, purpose, attempt)` | `PollingEvaluationExecutor(stage_output=...)` and `SemanticSlurmEvaluationExecutor(stage_output=...)`, through `TryHandles.stage_output` |
| `vs_sandbox` `SlurmStageOutputOpener` | handle id, stage name | `SlurmEvaluationExecutor(stage_output=...)`; the semantic executor adapts its `StageOutputOpener` to it |
| `vs_runtime` `ToolBuildOutputOpener` | names of the evaluator tools built | `RunEnvironmentRequest.tool_build_output` |

The run's openers live in `vibesys.run.logs`. `stage_output_opener` names a stage
`GateStage(execution_id, gate, attempt)`: the measurement's handle (the `execution_id` of
its gate events), the gate `measurement_events.gate_of(purpose, stage_id)` gives (the
same mapping the gate events use), and the try number. A `RetryingEvaluationExecutor`
runs try `n` under a new inner handle, so the run shares one `TryHandles` between it and
the polling executor's opener, which names each run by the outer handle and try. A stage
that is no gate (profiling) opens no stream. `tool_build_output_opener` names a build
`ToolBuild(tool_build_name(tools))`: the tools sorted, at most four named, then `+N more`.

Every `CommandRunner.execute` takes `output` and writes each chunk its command prints to
it as the command runs; the bounded `CommandResult` does not change.

A Slurm stage runs on the cluster, where the batch script writes its stdout and stderr to
per-stage files (`job.log` holds the job's setup and service output, not a stage's).
`vs_slurm.api.SlurmFileSource` reads such a file as a `ByteSource` through the runner's
own transport: one framed remote command per call (`od` for the bytes, `ls -lni` after it
for inode and size, both POSIX), at most `MAX_REMOTE_READ_BYTES` (256 KiB) a read, so its hex
form stays under the transport's 1 MiB stream cap. `Cluster.stage_output(handle, index,
stream)` names a stage's file. `vs_sandbox`'s `StageOutputFollower` is the pump: a
library cannot import `vs_logs`, and the executor already polls the scheduler, so
each poll that finds a stage running opens its stream and copies at most four reads per
file, with no second loop. When the job is collected, the rest comes from the collected
stage files (no extra remote reads), and each stream ends `ended` when its stage ran to
an exit status, `failed` when it did not, was cancelled, or its file shrank or was
replaced. A stage that ran between two polls opens its stream then, or at settle.

`vibesys.run.logs.open_run_log_store(project_root, run_id, threads=..., warn=...)` opens a
run's store in `Project.log_streams_directory_for(root, run_id)`, the one place that path is
built (the host helper reads it there too). Opening a store closes as `abandoned` every
stream a crashed writer left open.

A run opens its store in one place: run assembly (`vibesys.run.resources`) enters
`vibesys.run.logs.open_run_logs` on the run's ownership stack, and hands the
`LogScope[LogOwner]` it yields to the run's producers. If the store cannot be opened,
or its index holds a complete record that does not decode (`CorruptLogError`), the run
goes on: a warning goes to stderr, and producers write to a bounded in-memory store nobody
reads. A store that fails later warns on stderr through the same `warn`.

## Narration

The run's own narration (Python `logging` records, `lprint` lines, uncaught
tracebacks) is one stream per run, under the `RunNarration` owner.

- `vs_logs.api.NarrationHandler` is a `logging.Handler`. It writes each record as one
  `NarrationLine` JSON object per line (`at`, `level`, `logger`, `thread`, `message`,
  `traceback`). Before a sink is attached it keeps the newest 1000 records and counts
  the rest; attaching writes them first, in order. If the sink fails, the handler warns
  once on stderr and stops; the caller never sees the failure.
- `entrypoints.detached_run` installs the handler on the root logger before it loads
  the engine. It sets the root level to `INFO` and keeps `logging.lastResort`, so
  stderr shows the warnings it always showed. It also installs `sys.excepthook` and
  `threading.excepthook` (`uncaught_hook`, `uncaught_thread_hook`), which add the
  traceback to the stream and then call the previous hook.
- `open_run_logs` finds that handler (`installed_narration()`) and attaches the
  narration stream until the run's resources close.
- `RunLogger.lprint` also logs each line, without ANSI escapes, to the `run` logger
  (`vs_project.api.LPRINT_LOGGER`) at `INFO`. A process that configures no logging
  drops those lines.

The JSON Lines framing is for the client that renders the stream. A plain-text line
cannot carry a multi-line traceback, or a level the client can style, without a second
parser. Product code never parses these lines back.

What the stream does not hold:

- Anything that dies before run assembly attaches the stream. Examples are a bad
  argument, an import error, or a project that does not resolve. It is either a typed
  launch failure on the registry record, or it reaches only `server.log`, through the
  previous excepthook.
- An exception that escapes the run after its resources closed. It reaches `server.log`
  only.
- A hard death (SIGKILL, the OOM killer, a native crash). A later listing records it
  as `server_crashed`, with no exit status or signal (see Status).

## On disk

```
streams/
  index.jsonl                      opened {stream_id, owner, command} and closed {stream_id, outcome, degraded?}
  000001/
    00000000000000000000.seg       raw bytes from offset 0
    00000000000016777216.seg       raw bytes from offset 16777216
    pipes.jsonl                    {at, pipe} where the writing pipe changed
```

Every byte has a monotonic offset from 0. Segments hold raw bytes and are named by the
offset of their first byte; only the newest grows. The cap (`LogLimits.max_bytes`, 64 MiB
by default) drops the oldest whole segments, so the earliest retained offset only moves
forward and every retained byte keeps its offset. A pipe mark is written before the bytes
it labels. Index and pipe marks are append-only JSON lines; a reader ignores a trailing
line without its newline. Sizes are not stored: they are the segments' sizes. A closed
record carries `degraded: true` only when the store lost some of the stream's bytes, and
an opened record from an older store may carry `adopted_path: null`, which is ignored.

## Reading

`vs_logs.read.open_log_directory(directory, owner)` returns a `LogReader`:
`list_streams()` and `read(stream_id, FromOffset(n) | Tail(n), max_bytes)`. A `LogChunk`
carries the bytes, the pipe spans, `truncated_before` (asked-for bytes were evicted),
`eof` and `closed`. The close state is read before the bytes, so `closed` with `eof`
means the chunk reaches the final end. Every byte is read through `ByteSource`. A
`StreamInfo` says `degraded` when the store lost some of the stream's bytes; the wire's
`logs.list` does not carry it yet. A listing or read raises `CorruptLogError` when the
index holds a complete record that does not decode; the host helper answers that with
`log_store_corrupt`, which the client does not retry on its own.

## Enforcement

| Rule | Enforced by |
| --- | --- |
| No library imports `vs_logs` | tach layer `logs`, above `prompts_and_lower_libs` |
| Only the host helper reads logs | tach `[[interfaces]]` for `vs_logs.read`, `visibility = ["server.host_helper_os"]` (its real-host `StoreLogReader`) |
| Libraries cannot name owners | `LogOwner` lives in `vibesys`, which libraries cannot import |
| Unknown owner keys and kinds are rejected | `extra="forbid"` and the discriminated union, `tests/vibesys/run/test_run_logs.py` |
| Our own processes' output is never parsed for facts | Not lintable: a parse at a foreign tool's call site looks the same. Review. |
| Every store implementation behaves alike | `vs_logs.api.testing.LogStoreContract` against `FileLogStore` and `MemoryLogStore` |
| A log failure never fails or stalls the work | `LogStream` catches every volume error; `libs/vs-logs/tests/test_store_failures.py` (a `MemoryVolume` that fills up) and `tests/vibesys/run/test_log_failures.py` (a local command, a cancel), plus the image build and Slurm cases beside their producers |
| Every runner call chooses where its output goes | The type checker: `output: OutputSink` has no default on `Processes.run`, `StreamingCommand.run`, `DockerCli.run`, `DockerBuildRunner.run`, `CommandRunner.execute` and `start`. `DISCARD` is the explicit opt-out for a probe whose output is parsed or only shown in an error. |

## Status

| Part | Status |
| --- | --- |
| `vs_sim` sinks and `ByteSource` | built |
| `vs_logs` store, scope, reader; `LogOwner` and its TypeScript | built |
| Startup producers (image build, container start) | built |
| Host helper `logs` feature and the client logs lib | built |
| Narration handler and excepthook | built |
| `NarrationLine` in the generated protocol types | built |
| Run view Startup log panel and Inspect log sheet | built |
| Terminal escape sequences (colors, cursor moves, titles) dropped by the client's fold, across chunk boundaries, so a log view renders text only | built: `clients/libs/logs/src/escapes.ts`, property tests in `escapes.test.ts` |
| Gate stage and tool build openers in the libraries | built |
| `vibesys` openers for gate stages and tool builds | built for in-process stages; the host environment's hook-driven tool install still discards |
| SkyPilot stage output | built: streams through the command runner sink |
| Slurm stage output | built: live per poll |
| Explicit `output` on every runner call | built: the argument has no default, so the type checker rejects a call that omits it; the long-running calls that still pass `DISCARD` are the places to give an owner next |

Known gaps:

- Owners for agent invocations and Slurm jobs are removed until something writes them (a
  Slurm stage writes under its `GateStage`).
- `degraded` is on the index and `StreamInfo`, not yet on the wire's `logs.list`.
- The CLI `build_task_image` is unlogged.
- `DockerSandbox` restart discards its output.
- `job.log` and the in-container dockerd log are not followed.
- A hard death's exit status or signal is not recorded on the tombstone. The detached
  server is reparented to init, so no vibesys process waits on it; recording it needs a
  supervisor.
- The `run-*.log` files and the stderr tee are not yet retired.
- The deprecated inspect `include_log` tail is removed at helper protocol v2.
- The Slurm `service_log_tail` is still pasted into failure text. It should become a log
  reference.
