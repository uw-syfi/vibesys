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
| Owners | `vibesys.log_owners` (`LogOwner`) | What each stream is for, as a typed reference to the run model. |
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
exception leaves it. `LogStore.adopt(path, owner, command)` lists a file someone else
writes (a Slurm job's output file), read as-is. `follow(source, sink, threads=..., stop=...)`
copies a growing `ByteSource` (a file in a container or on a cluster) into a sink.

A library never sees a scope or an owner. It takes an opener in its own vocabulary, a
factory that returns a context manager of an `OutputSink`, calls it once per command or
stage, and passes the entered sink down per call to the runner that writes it:

| Opener | Called with | Carried by |
| --- | --- | --- |
| `vs_runtime` `StartupOutputOpener` | startup stage, command description | `RunEnvironmentRequest.startup_output` |
| `vs_runtime` `StageOutputOpener` | `StageRun(handle_id, stage_id, purpose, attempt)` | `PollingEvaluationExecutor(stage_output=...)`, through `TryHandles.stage_output` |
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

`vibesys.run.logs.open_run_log_store(project_root, run_id, threads=...)` opens a run's
store in `Project.log_directory_for(root, run_id) / "streams"`. Opening a store closes as
`abandoned` every stream a crashed writer left open.

A run opens its store in one place: run assembly (`vibesys.run.resources`) enters
`vibesys.run.logs.open_run_logs` on the run's ownership stack, and hands the
`LogScope[LogOwner]` it yields to the run's producers. If the store cannot be opened,
the run goes on: a warning goes to stderr, and producers write to a bounded in-memory
store nobody reads.

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
  index.jsonl                      opened {stream_id, owner, command, adopted_path} and closed {stream_id, outcome}
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
line without its newline. Sizes are not stored: they are the segments' sizes.

Store-owned and adopted streams share the format: an adopted file is one segment at
offset 0 with no pipe marks.

## Reading

`vs_logs.read.open_log_directory(directory, owner)` returns a `LogReader`:
`list_streams()` and `read(stream_id, FromOffset(n) | Tail(n), max_bytes)`. A `LogChunk`
carries the bytes, the pipe spans, `truncated_before` (asked-for bytes were evicted),
`eof` and `closed`. The close state is read before the bytes, so `closed` with `eof`
means the chunk reaches the final end. Every byte is read through `ByteSource`.

## Enforcement

| Rule | Enforced by |
| --- | --- |
| No library imports `vs_logs` | tach layer `logs`, above `prompts_and_lower_libs` |
| Only the host helper reads logs | tach `[[interfaces]]` for `vs_logs.read`, `visibility = ["server.host_helper_os"]` (its real-host `StoreLogReader`) |
| Libraries cannot name owners | `LogOwner` lives in `vibesys`, which libraries cannot import |
| Unknown owner keys and kinds are rejected | `extra="forbid"` and the discriminated union, `tests/vibesys/run/test_run_logs.py` |
| Our own processes' output is never parsed for facts | Not lintable: a parse at a foreign tool's call site looks the same. Review. |
| Every store implementation behaves alike | `vs_logs.api.testing.LogStoreContract` against `FileLogStore` and `MemoryLogStore` |

## Status

| Part | Status |
| --- | --- |
| `vs_sim` sinks and `ByteSource` | built |
| `vs_logs` store, scope, reader, follow; `LogOwner` and its TypeScript | built |
| Startup producers (image build, container start) | not built |
| Host helper `logs` feature and the client logs lib | not built |
| Narration handler and excepthook | built |
| `NarrationLine` in the generated protocol types | not built (with the client logs lib) |
| Exit status or signal of a hard death on the tombstone | not built: the detached server is reparented to init, so no vibesys process waits on it and can read its status |
| Gate stage and tool build openers in the libraries | built |
| `vibesys` openers for gate stages and tool builds | built (in-process stages; the host environment's hook-driven tool install still discards) |
| Remote followers (Slurm, SkyPilot, dockerd) | not built |
