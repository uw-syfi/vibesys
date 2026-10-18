# Stitch (experimental)

Stitch is an installable Rust CLI for assembling profiling evidence from
different sources into a source-linked execution graph. It is independent of
VibeSys's current profiler and MCP paths. This is an early draft implementation,
intended to support iterative work against Masa's Hotel application.

## Build and run

```sh
cd experimental/stitch
cargo install --path crates/stitch-cli
stitch plan examples/small/manifest.json
stitch ingest examples/small/manifest.json --out /tmp/stitch-graph.json
stitch explain /tmp/stitch-graph.json example-trace
stitch costs /tmp/stitch-graph.json
```

`stitch capture --pid PID --seconds 10 --out /tmp/stitch-capture` initiates a
Linux `perf record` capture for a running process, exports leaf CPU samples via
`perf script`, and writes a manifest. It requires a working `perf` installation
and the host's permission to profile the selected process. It rejects captures
with no CPU samples and retains the raw perf artifacts for diagnosis. Run
`stitch ingest /tmp/stitch-capture/manifest.json --out /tmp/graph.json` and
`stitch costs /tmp/graph.json` to inspect a stack tree. The tree reports
sample counts, not measured CPU time. Add OTel and dial9 inputs to the manifest
to correlate them with the perf samples.

## Workspace

| Crate | Responsibility |
| --- | --- |
| `stitch-core` | Pure evidence model, graph construction, and trace queries |
| `stitch-sources` | Artifact importer interface and OTel, perf, dial9 bridge, and GPU bridge importers |
| `stitch-mapping` | Source attribution interface and declared-location resolver |
| `stitch-session` | Manifest validation, capture lifecycle, import wiring |
| `stitch-cli` | CLI commands and presentation |

Importers translate source-specific artifacts into typed observations. The core
accepts importers through a registry keyed by format, and source mappers through
a separate registry; both can be extended without changing the graph code. The core
joins OTel spans by trace and parent span IDs. A dial9 task poll links to a span
only when the exported record carries the matching trace and parent span IDs. A
perf sample links to a poll only with the same process, thread, and declared
clock domain, and only when exactly one poll contains the sample timestamp.
Unassigned samples remain visible in the graph. A source resolver maps explicit
file and line hints to a configured source tree; the attribution records which
resolver and basis made that claim. Instruction-address symbolization, generated
language source maps, and runtime frame mapping belong in this separate plugin
family, rather than in profiler importers.

## Input formats and current limits

`RunManifest` is JSON with `schema_version: 1`, a label, and an `inputs` array.
Each input has `format`, `path`, and `clock`. Relative paths resolve against the
manifest's directory. Optional `source_root` resolves the same way;
`source_revision` is the source revision supplied by the operator. Unknown
manifest and bridge-record keys are rejected.

Supported `format` values:

- `otlp_json`: OTLP JSON `resourceSpans` with `service.name` attributes.
- `perf_script`: `perf script --ns -F pid,tid,time,ip,sym` output, including
  indented call-chain frames.
- `dial9_bridge_jsonl`: one normalized task-poll record per line, with task,
  poll, process, thread, interval, optional trace context, and optional file and
  line. This is a provisional bridge format, **not** native dial9 binary input.
- `gpu_bridge_jsonl`: a provisional normalized GPU-kernel event format, **not**
  a native vendor profiler import.

The current CLI can initiate perf capture for one PID. OTel and dial9 must
already be configured in the target application to emit artifacts; Stitch does
not yet initiate their in-process recording. Clock labels are assertions by the
operator: Stitch does not yet calibrate clocks. Its CPU report counts samples;
it does not claim exact CPU time or source lines for perf frames. A source
revision supplied in a manifest is not independently verified. Native dial9
decoding, GPU vendor capture, distributed capture coordination, binary debug
symbol resolution, and VibeSys trusted-evidence acceptance are follow-up work.

The first live application check used Masa Hotel's recommendation service with
gRPC requests. Stitch captured and ingested its perf call chains and produced a
sample cost tree. This is a one-service integration check, not a distributed
Hotel run or an optimization result. Hotel currently emits neither OTLP spans
nor native dial9 traces in that setup, so cross-source correlation on Hotel
remains unverified.
