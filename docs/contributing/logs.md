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
| Serving | `server.host_helper` (`logs` feature) | Chunk caps, long-poll follow, discovery. |
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

`vibesys.run.logs.open_run_log_store(project_root, run_id, threads=...)` opens a run's
store in `Project.log_directory_for(root, run_id) / "streams"`. Opening a store closes as
`abandoned` every stream a crashed writer left open.

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
| Only `server.host_helper` reads logs | tach `[[interfaces]]` for `vs_logs.read`, `visibility = ["server.host_helper"]` |
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
| Narration handler and excepthook | not built |
| Gate stages, tool builds, remote followers (Slurm, SkyPilot, dockerd) | not built |
