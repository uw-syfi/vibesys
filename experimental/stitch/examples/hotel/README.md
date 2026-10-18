# Hotel integration and application effort

This example profiles Masa Hotel with an opt-in `stitch_profile` feature and
three simultaneous sources: OpenTelemetry SDK spans, native dial9 custom
events, and Linux perf samples. The feature keeps the normal Hotel build free
of profiling code. Stitch reads the resulting files after the processes exit.

## Work required in the application

| Source | Application changes | Runtime setup | What Stitch receives |
| --- | --- | --- | --- |
| perf | None. Build with symbols and frame pointers for better stacks. | PID, `perf` binary, and permission to profile it. | Sampled CPU call chains with process and thread IDs. |
| OpenTelemetry | Add SDK/exporter dependencies, initialize a provider, and place spans around selected operations. Propagate trace context across services to connect an end-to-end request. | Select an exporter destination and service identity. Hotel's example writes one OTLP JSON document per line. | Request spans and explicit trace/span IDs. |
| dial9 | Add the dial9 recorder and a custom CPU-region event at the selected source point. Copy the active OpenTelemetry IDs into the event and include PID, TID, monotonic timestamps, and a file/line hint. | Select a trace directory and shut down cleanly so dial9 seals its binary file. | CPU regions linked to spans, samples, and the declared source point. |

The Hotel example adds a shared profiling module and opt-in server wiring in
frontend, search, geo, rate, reservation, profile, and user. The server layer
extracts incoming trace context, creates a request span, and records dial9
regions for each poll of that RPC future. Frontend and search explicitly copy
the trace context to their child RPCs. Every service needs exporter setup and
every outgoing RPC edge needs propagation; the client starts a root trace at
frontend. Recommendation has separate instrumentation and is profiled as a
standalone endpoint below. Review's frontend handler is a stub in this checkout.

Masa's Tokio fork is 1.37. Current dial9 runtime hooks target newer Tokio, so
the Hotel feature records dial9 **custom CPU regions** around each instrumented
RPC future poll (and the synchronous recommendation handler). These are genuine
dial9 binary traces, but they do not contain Tokio task spawn, wake, or queue
events. Full dial9 Tokio coverage requires upgrading or backporting hooks in
Masa's Tokio fork. These regions are deliberately modeled as `cpu_region`, not
Tokio `task_poll`. Their declared source location currently identifies the
service file, not the exact handler line.

## Run the example

Build Hotel's frontend, search, geo, rate, reservation, profile, and user
binaries with `--features stitch_profile`. Start each with a separate
`STITCH_PROFILE_DIR`, then drive the Search and Reservation frontend RPCs with
Hotel's load generator. Capture perf across all seven service PIDs and the
backing datastore PIDs during the workload. Shut down the services cleanly to
seal each `dial9.0.bin` file. Create a Stitch manifest with each service's
`otel.jsonl` as `otlp_jsonl` on `unix`, each `dial9.0.bin` as
`dial9_stitch_bin` on `monotonic`, and the combined `perf.script` as
`perf_script` on `monotonic`. Set `source_root` to the Hotel checkout and run
`stitch ingest MANIFEST --out GRAPH`, then `stitch costs GRAPH` and
`stitch explain GRAPH TRACE_ID`.

For the separate recommendation run, build `hotel_recommendation` with
`--features stitch_profile`, then start it
with `STITCH_PROFILE_DIR` set to a new directory. Generate recommendation RPC
traffic with Hotel's `hotel_recommendation_stitch_load` binary, which reuses one
gRPC connection. Direct profile output to memory-backed storage if available;
the profile exporter writes synchronously for each request. Use `stitch capture --pid PID --seconds 12
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

For the measurement below, `perf record --call-graph dwarf,8192` and
`perf script --no-inline --ns -F pid,tid,time,ip,sym` gave substantially more
complete stacks than frame-pointer capture. Stitch now selects DWARF by
default and offers `--stack-mode frame-pointer` when the target build retains
frame pointers. The `--no-inline` option also avoided a `perf script`
addr2line error on this Hotel binary.

## Whole-application live validation

The current frontend exposes two active paths. The Search path calls search,
which calls geo then rate, followed by reservation availability and profile.
The Reservation path calls user authentication then reservation creation when
authentication succeeds. We ran both paths through frontend at 50 requests per
second, with a 75/25 Search/Reservation mix, against all seven services, four
MongoDB processes, and three Redis processes on one host. A five-second warmup
preceded the 25-second measured load. Perf sampled every service and datastore
process at 199 Hz for 15 seconds. These measurements describe this workload;
the standalone recommendation endpoint below is a separate run.

The simultaneous three-source capture produced 8,084 OpenTelemetry spans,
31,672 dial9 poll regions, and 7,555 perf samples. All 6,522 child spans have
an explicit parent link; all regions link to a span. Stitch assigned 2,640
samples to exactly one poll region by process, thread, and monotonic interval.
The other 4,915 samples remain in the process cost tree. For one Search trace
inside the perf interval, `stitch explain` returned all six service spans,
their poll regions, and 11 joined CPU samples. Declared source attribution
resolves to a service source file; perf frames are not yet resolved to exact
source lines.

### Request model and latency cost tree

The tree below shows median inclusive span duration from the profiled run,
including warmup requests. Sibling durations are sequential on these paths;
the figures are medians of each span, so they must not be summed into an exact
request median. An RPC span includes waiting, while perf samples represent CPU
use. The frontend exclusive time is its inclusive time minus child spans per
trace, then summarized by median.

```text
Search (1,158 traces): frontend HandleSearch 154.64 ms; exclusive 15.82 ms
  search HandleNearby 17.32 ms; exclusive 11.91 ms
    geo GetNearby 0.50 ms
    rate GetRates 4.86 ms
  reservation CheckAvailability 118.62 ms
  profile GetProfiles 3.28 ms

Reservation (404 traces): frontend HandleReservation 24.29 ms; exclusive 9.80 ms
  user CheckUser 3.23 ms
  reservation MakeReservation 11.88 ms (328 traces; authenticated path)
```

Reservation availability consumed a median 76.9% of its parent Search duration
on a per-trace basis. Search p95 was 180.95 ms and availability p95 was 140.16
ms. The client measured 953 successful Search requests with p50/p95 of
158.4/178.7 ms, and 298 successful Reservation requests with p50/p95 of
31.3/67.1 ms. The difference between client and span figures reflects their
different measurement boundaries and inclusion of warmup traces.

### CPU cost tree

For a separate, uninstrumented run of the same mixed workload, perf collected
7,041 CPU samples across all 14 application and datastore processes. This is
the lower-overhead ranking to use for CPU decisions; the three-source run is
for correlation. A sample represents observed on-CPU work, not elapsed request
time. The shares below are mutually exclusive by process:

| Process group | Samples | Share |
| --- | ---: | ---: |
| reservation | 2,262 | 32.1% |
| frontend | 1,345 | 19.1% |
| rate | 954 | 13.5% |
| search | 848 | 12.0% |
| profile | 653 | 9.3% |
| geo | 342 | 4.9% |
| user | 158 | 2.2% |
| MongoDB and Redis, combined | 479 | 6.8% |

Within reservation, 1,007 of 2,262 samples included `check_availability`,
617 included `chrono::format`, and 535 included Redis client frames. These
stacks overlap, so their counts must not be added. In the source, reservation
parses the same input dates once per hotel and formats dates once per day, then
performs cache and database work. Date conversion is a plausible CPU target
after removing the latency blocker; a dedicated before/after CPU measurement
is needed to quantify its benefit.

### Action supported by the profile

`check_availability` ends with an unconditional 100 ms async sleep. This sleep
uses little CPU and therefore would be missed by a CPU-only ranking. To test
causality, we removed only that sleep in a temporary build and reran the same
50-request-per-second mixed load. Search client p50/p95 fell from 158.4/178.7
ms to 56.3/79.6 ms; Reservation remained at 30.6/69.9 ms versus 31.3/67.1
ms. The temporary source edit was restored. The first actionable change is to
remove the sleep if it is not part of the intended service behavior, or make
the delay an explicit workload parameter if it is. Then reprofile reservation
CPU and date conversion. This is one local controlled comparison, not an
estimate for a production deployment.

## Standalone recommendation validation

The captured fixture files in `fixtures/` are from a real Hotel recommendation
request. Tests import those files through Stitch's public importer interface.
The full live run and measured counts are recorded in the pull request.

### What the profile says

A localhost load generator reused one gRPC connection and sent the `dis`
recommendation request repeatedly. The server ran an unoptimized build with
stdout directed to `/dev/null`; profiled output went to `/dev/shm`. A 12-second
perf capture of the uninstrumented service contained 705 CPU samples. Among
them, 552 (78.3%) had gRPC/HTTP2/protobuf protocol frames and no more specific
recommendation, logging, or network category; 20 (2.8%) entered the
recommendation calculation, including 10 with Haversine distance frames.
HTTP2 HPACK appeared in 171 samples (24.3%), framing in 147 (20.9%), and
stream management in 249 (35.3%). Those three counts overlap and must not be
added. The main observed CPU cost for this endpoint is RPC protocol processing,
not the hotel-distance scan. This is a single endpoint under one localhost
workload, so it does not rank the full Hotel application.

The simultaneous three-source run produced 12,379 OpenTelemetry spans,
12,379 dial9 CPU regions, and 1,009 perf samples. Stitch linked every span to
its region and assigned 92 samples to handler regions by PID, TID, and
monotonic time. Only the synchronous handler body is marked as a region;
unassigned samples still contribute to the process cost tree. In a controlled
5,000-request comparison, the same client took 13.017 seconds without the
profile feature and 16.934 seconds with it (30.1% longer). Therefore use the
uninstrumented perf run for a production-cost ranking and the three-source run
for correlation and source-point navigation. This one-run timing comparison is
diagnostic, not a performance benchmark.
