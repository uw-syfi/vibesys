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

## OTel traces on Kubernetes

Stitch can capture OpenTelemetry traces from a microservice application on
Kubernetes and render the latency analysis as text. It shells out to `kubectl`,
which honors `KUBECONFIG` and the current context; `--kubectl PATH` selects
another binary.

```sh
stitch capture otel-k8s start --namespace hotel \
  --deployment frontend --deployment search --deployment geo \
  [--sampler-ratio 1.0] [--collector-image IMG] [--sidecar-image IMG] \
  [--timeout-seconds 600] [--kubectl PATH]
# ... run the load phase ...
stitch capture otel-k8s collect --namespace hotel --out /tmp/otel \
  [--settle-seconds 5] [--window START_UNIX_NANOS:END_UNIX_NANOS[:LABEL] ...] \
  [--label LABEL] [--source-root PATH] [--kubectl PATH]
stitch ingest /tmp/otel/manifest.json --out /tmp/otel/graph.json
stitch render /tmp/otel/graph.json --out /tmp/otel/render \
  [--window LABEL|all] [--top 20] [--max-roots 10] [--max-nodes 30] [--width 48]
```

`start` applies a `ConfigMap`, Deployment and Service named
`stitch-otel-collector` into the namespace. The collector
(`otel/opentelemetry-collector-contrib` 0.159.0, pinned by digest) receives
OTLP gRPC on 4317, OTLP HTTP on 4318 and Jaeger compact thrift on UDP 6831, and
its `file` exporter writes OTLP JSON lines to `/data/otel.jsonl` on an
`emptyDir`. A `memory_limiter` keeps it under its 512 MiB limit; under overload
it refuses spans rather than being OOM-killed. A `busybox` `reader` sidecar
(pinned by digest) mounts the same volume, because the collector image has no
shell. Once the collector is ready, `start` sets these variables on every
container of each named Deployment and waits for each rollout:
`OTEL_EXPORTER_OTLP_ENDPOINT=http://stitch-otel-collector.NS.svc.cluster.local:4317`,
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`, `OTEL_TRACES_SAMPLER=parentbased_traceidratio`,
`OTEL_TRACES_SAMPLER_ARG`, and for `jaeger-client` applications
`JAEGER_AGENT_HOST`, `JAEGER_AGENT_PORT=6831`, `JAEGER_SAMPLER_TYPE=probabilistic`
and `JAEGER_SAMPLER_PARAM`. Only the named Deployments restart. It checks that
every Deployment exists before it changes anything, bounds all waiting by
`--timeout-seconds`, and prints one JSON object:
`{"namespace", "collector_service", "deployments", "sampler_ratio", "started_unix_nanos"}`,
where `collector_service` is the Service's DNS name and `started_unix_nanos`
is taken after the last rollout finished.

`collect` waits `--settle-seconds` for the collector to flush, reads the file
with `kubectl exec ... -c reader -- cat`, drops a final line that is still
being written, and writes `DIR/otel.jsonl` and `DIR/manifest.json` (input
format `otlp_jsonl`, clock `wall`, and the `--window`s). It prints one summary
line and fails if the export has no spans, keeping the file for diagnosis. It
leaves the collector running; deleting the namespace removes it.

`render` writes, into its output directory:

- `README.txt`: what each file is, the windows, span and trace counts, and the
  traces excluded from the call graph by reason.
- `report.txt` and `report.json`: span, trace and error counts and latency
  tables (count, errors, mean, p50, p95, p99, max) by service, by
  `service: operation`, and by datastore call (client spans with `db.system`),
  sorted by p95 and bounded by `--top`.
- `tree.txt`: per root endpoint, the aggregated call graph as an indented
  tree. A client span and the server span it caused are one hop that shows
  client time, server time, and their difference (network and queueing).
- `critical-path.txt`: per root endpoint, nodes ranked by critical-path
  contribution (share of root wall time, ms per request) and the critical path
  of the p95-representative trace.
- `waterfall.txt`: per root endpoint, the traces closest to p50 and p95 as
  text waterfalls.
- `graph-summary.json`: roots, nodes, edges, critical paths and
  representative traces, unbounded.

The analysis is a port of the evaluator's Go telemetry package
(`wall_clock_active_leaf_v1`) and a test holds it to numbers the Go code
produced for a shared fixture. A trace enters the call graph only if it has
one root, its root and every span lie in one window, every parent is present
and contains its children, span IDs are unique, and there is no cycle. The
latency tables count every span inside the windows.

Limits: the capture is one collector replica writing one file; a restart of the
collector pod loses what it had written. Spans are sampled at the SDK by
`--sampler-ratio`, so counts are of sampled requests. Clock skew between nodes
shows up as `clock_inconsistency` exclusions. Operation names follow the
evaluator's rules (`http.route` with `http.request.method`,
`rpc.service/rpc.method`, `db.system:db.operation.name`, else the span name
without its query string); a span name that embeds IDs raises cardinality.
Producer/consumer and link relationships are counted but are not on the
critical path.

## Workspace

```text
crates/
  stitch-core/             evidence graph and pure correlation
  stitch-analysis/         trace analysis and its text rendering
  stitch-importer-api/     profiler importer contract and registry
  stitch-source-api/       source mapper contract and registry
  stitch-session/          manifest and import orchestration
  stitch-cli/              commands and built-in plugin selection
plugins/
  profilers/
    otel/                  OTLP JSON and JSONL; OTel capture on Kubernetes
    perf/                  Linux perf capture and script import
    dial9/                 native dial9 and provisional bridge
    gpu-bridge/            provisional GPU event bridge
  source/
    declared-workspace/    declared file and line hints
```

Each profiler integration and each source mapping implementation has its own
crate. Format variants from the same profiler share a crate. Plugins depend on
the small importer or source mapper contract and on `stitch-core`; the perf and
OTel plugins also use the session manifest contract for capture output. The
analysis crate depends only on `stitch-core`. The core and
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
Each input has `format`, `path`, and `clock`. An optional `windows` array holds
measurement windows, each `{"start_unix_nanos", "end_unix_nanos", "label"?}`
with unique labels; `ingest` copies them into the graph and `render` selects
spans with them. Relative paths resolve against the manifest's directory. Optional `source_root` resolves the same way;
`source_revision` is the source revision supplied by the operator. Unknown
manifest and bridge-record keys are rejected.

Supported `format` values:

- `otlp_json`: one OTLP JSON document with `resourceSpans` and `service.name`.
- `otlp_jsonl`: one OTLP JSON document per line, for a streaming file exporter.
  Lines are decoded one at a time; span kind and status may be integers or
  enum names. A span ID repeated within a trace is kept, and the analysis
  excludes that trace.
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

The current CLI can initiate perf capture for one PID and OTel capture for
Deployments on Kubernetes whose applications already use an OTel or Jaeger SDK
configured from the standard environment variables. dial9 must already be
configured in the target application; Stitch does not initiate its recording. Clock labels are assertions by the
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
