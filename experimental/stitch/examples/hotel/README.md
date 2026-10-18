# Hotel integration and application effort

This example profiles Masa Hotel's recommendation service. It uses an opt-in
`stitch_profile` feature in Hotel and three simultaneous sources: OpenTelemetry
SDK spans, native dial9 custom events, and Linux perf samples. The feature keeps
the normal Hotel build free of profiling code. Stitch reads the resulting files
after the process exits.

## Work required in the application

| Source | Application changes | Runtime setup | What Stitch receives |
| --- | --- | --- | --- |
| perf | None. Build with symbols and frame pointers for better stacks. | PID, `perf` binary, and permission to profile it. | Sampled CPU call chains with process and thread IDs. |
| OpenTelemetry | Add SDK/exporter dependencies, initialize a provider, and place spans around selected operations. Propagate trace context across services to connect an end-to-end request. | Select an exporter destination and service identity. Hotel's example writes one OTLP JSON document per line. | Request spans and explicit trace/span IDs. |
| dial9 | Add the dial9 recorder and a custom CPU-region event at the selected source point. Copy the active OpenTelemetry IDs into the event and include PID, TID, monotonic timestamps, and a file/line hint. | Select a trace directory and shut down cleanly so dial9 seals its binary file. | CPU regions linked to spans, samples, and the declared source point. |

The Hotel example adds one small profiling module and wiring in the
recommendation binary and handler. Other services need their own source points
and exporter setup. A distributed trace also needs incoming context extraction,
outgoing context injection, and consistent service names at every RPC boundary;
this one-service check does not add that propagation.

Masa's Tokio fork is 1.37. Current dial9 runtime hooks target newer Tokio, so
the Hotel feature records a dial9 **custom CPU region** inside the synchronous
recommendation handler. This is a genuine dial9 binary trace, but it does not
contain Tokio task spawn, wake, queue, or complete poll events. Full dial9 Tokio
coverage requires upgrading or backporting hooks in Masa's Tokio fork. The
region is deliberately modeled as a `cpu_region`, not a Tokio `task_poll`.

## Run the example

Build `hotel_recommendation` with `--features stitch_profile`, then start it
with `STITCH_PROFILE_DIR` set to a new directory. Generate recommendation RPC
traffic while the process runs. Use `stitch capture --pid PID --seconds 12
--out PERF_DIR` during that traffic. Send SIGINT to Hotel after capture so the
dial9 recorder seals `dial9.0.bin`.

Create a Stitch manifest with three inputs: `otel.jsonl` as `otlp_jsonl` on
`unix`, `dial9.0.bin` as `dial9_stitch_bin` on `monotonic`, and
`PERF_DIR/perf.script` as `perf_script` on `monotonic`. Set `source_root` to the
Hotel checkout. Then run `stitch ingest MANIFEST --out GRAPH` and
`stitch costs GRAPH`.

The two monotonic sources can be joined by PID, TID, and timestamp. The Unix
OpenTelemetry span clock joins to dial9 through the explicit trace and span
IDs, so Stitch does not guess a wall-clock offset. The source line on a dial9
region is a declared location. Perf frames are not yet resolved through debug
information to source lines.

## Live validation

The captured fixture files in `fixtures/` are from a real Hotel recommendation
request. Tests import those files through Stitch's public importer interface.
The full live run and measured counts are recorded in the pull request.
