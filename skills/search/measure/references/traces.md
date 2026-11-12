# Trace jobs

A microservice task whose manifest has a `[trace]` section can be traced per commit:

```
vibesys-cli submit trace <sha> [--note "<why>"]
vibesys-cli wait <job>
vibesys-cli show <sha>        # each saved trace of the commit (newest last): its artifacts directory and files
```

The job builds and deploys the commit as a benchmark sample does, records every request's
OpenTelemetry spans (sampler ratio 1) with stitch over a short workload of the task's choosing
(hotel-k8s: a closed loop at saturation, then an open loop at 70% of it; one window each),
and writes stitch's reports. No correctness check runs and no score is kept. A second submit
for the same commit and task version returns the saved trace; `--no-cache` traces it again.

## Where the reports are

`<artifacts>/trace/`, where `show <sha>` prints `artifacts:`
(`.vibesys-state/.artifacts/<task>/<job>/NN-trace`); `vibesys-cli jobs --kind trace` lists the traces:

| File | Read it for |
| --- | --- |
| `README.txt` | Start here: windows, span and trace counts, traces excluded and why, and whether the trace is complete |
| `critical-path.txt` | Per endpoint (root service and operation): count, p50/p95/p99, and the nodes ranked by what they contribute to the request's wall time (ms per request and share) |
| `tree.txt` | Per endpoint, the call graph: which service calls which, how often per request, client time against server time (the gap is network and queueing), and each node's self time |
| `report.txt` | Services, spans and datastore calls ranked by p95 |
| `waterfall.txt` | Representative requests (near p50 and p95) span by span: what runs in sequence and what overlaps |
| `*.json`, `graph.json`, `capture/otel.jsonl.gz` | The same as data, and the raw spans, for a question the text does not answer (`stitch render graph.json --out DIR --window phase-1` re-renders one window) |

## Reading it

- Bound a change by its node's critical-path contribution, not by its inclusive or aggregate
  latency: a service that is expensive in aggregate may hold no request open, and two
  overlapping calls of 10 ms each may cost 10 ms together.
- Read each window on its own: saturation (the closed loop) shows queueing at the bottleneck;
  the moderate load shows the request's own path.
- Every request is recorded, which costs throughput: compare traces with traces (base against
  candidate, same task version), never a trace's numbers with a measurement's.
- Cite the job id and the file in plans and findings, with the numbers you used.
- A trace job that fails says why in `README.txt` (or `vibesys-cli logs <job>`); a commit whose
  services do not export spans fails it with "no spans".
