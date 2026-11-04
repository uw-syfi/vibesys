# Stitch (experimental)

Stitch is an installable Rust CLI for assembling profiling evidence from
different sources into a source-linked execution graph. It is independent of
VibeSys's current profiler and MCP paths. This is an early draft implementation
for heterogeneous application profiling.

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

```text
crates/
  stitch-core/             evidence graph and pure correlation
  stitch-importer-api/     profiler importer contract and registry
  stitch-source-api/       source mapper contract and registry
  stitch-session/          manifest and import orchestration
  stitch-cli/              commands and built-in plugin selection
plugins/
  profilers/
    otel/                  OTLP JSON and JSONL
    perf/                  Linux perf capture and script import
    dial9/                 native dial9 and provisional bridge
    gpu-bridge/            provisional GPU event bridge
  source/
    declared-workspace/    declared file and line hints
```

Each profiler integration and each source mapping implementation has its own
crate. Format variants from the same profiler share a crate. Plugins depend on
the small importer or source mapper contract and on `stitch-core`; the perf
plugin also uses the session manifest contract for capture output. The core and
session never depend on a concrete plugin. The CLI registers the built-in
plugins and passes their registries to the session. A new built-in plugin adds
one crate and one CLI registration; this is compile-time Rust registration, not
dynamic library loading. Importers translate artifacts into typed observations.
The core joins OTel spans by trace and parent span IDs. A dial9 CPU region or task poll
links to a span only when its record carries the matching trace and parent span
IDs. A perf sample links to a CPU region or poll only with the same process, thread, and declared
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

- `otlp_json`: one OTLP JSON document with `resourceSpans` and `service.name`.
- `otlp_jsonl`: one OTLP JSON document per line, for a streaming file exporter.
- `perf_script`: `perf script --ns -F pid,tid,time,ip,sym` output, including
  indented call-chain frames.
- `dial9_bridge_jsonl`: one normalized task-poll record per line, with task,
  poll, process, thread, interval, optional trace context, and optional file and
  line. This is a provisional bridge format, **not** native dial9 binary input.
- `dial9_stitch_bin`: native dial9 binary events from an application Stitch CPU
  region. This is a real dial9 recording, but it does not provide dial9's full
  Tokio runtime hook coverage.
- `gpu_bridge_jsonl`: a provisional normalized GPU-kernel event format, **not**
  a native vendor profiler import.

The current CLI can initiate perf capture for one PID. OTel and dial9 must
already be configured in the target application to emit artifacts; Stitch does
not yet initiate their in-process recording. Clock labels are assertions by the
operator: Stitch does not yet calibrate clocks. Its CPU report counts samples;
it does not claim exact CPU time or source lines for perf frames. A source
revision supplied in a manifest is not independently verified. Full Tokio task
event decoding, GPU vendor capture, distributed capture coordination, binary debug
symbol resolution, and VibeSys trusted-evidence acceptance are follow-up work.

The [small example](examples/small/manifest.json) demonstrates a manifest with
OpenTelemetry, dial9 bridge, and perf inputs. A captured native dial9 artifact
under `crates/stitch-cli/tests/fixtures/` exercises the binary importer.

## Checks

Changes anywhere under `experimental/stitch/` select the `stitch` CI job through
repoctl. The job checks formatting, Clippy's default and pedantic lints plus
`redundant_clone` with warnings denied, cargo-strata's crate dependency policy,
and the workspace tests. The policy in [`strata.toml`](strata.toml) assigns every
crate a layer, limits each crate's direct dependencies including test-only
dependencies, and requires every crate root to forbid unsafe Rust code.

Run the same checks locally with:

```sh
cargo install cargo-strata --version 0.4.0 --locked
./support/repoctl/repoctl run-checks --group stitch_checks
```

Run the repoctl command from the repository root.
