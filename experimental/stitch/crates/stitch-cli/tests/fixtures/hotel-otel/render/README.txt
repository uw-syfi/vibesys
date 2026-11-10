STITCH TRACE ANALYSIS

Files (read the .txt files first; the .json files hold the same data unbounded):
  report.txt          span, trace and error counts; latency tables by service, by
                      `service: operation` and by datastore call, sorted by p95
  report.json         the same tables, unbounded
  tree.txt            per root endpoint, the aggregated call graph with client/server hops
  critical-path.txt   per root endpoint, nodes ranked by critical-path contribution
  waterfall.txt       per root endpoint, the p50 and p95 representative traces
  graph-summary.json  roots, nodes, edges, critical paths and representatives

Windows: measure [1700000000500000000, 1700000003000000000] (2.5 s)
Spans in windows: 256  traces: 20  span errors: 1
Traces captured: 20  eligible for the call graph: 17  excluded: 3
Exclusions by reason:
  clock_inconsistency          1
  duplicate_span_id            1
  missing_parent               1
Client/server pairs: 58  unmatched client spans: 77  unmatched server spans: 0  async relationships: 0
Root endpoints: 2 (text files show up to --max-roots 10)

Counts are of sampled spans: with a sampler ratio below 1 they are a sample of requests.
Exclusion reasons: duplicate_span_id (a span ID repeats in a trace), multiple_roots,
missing_parent (a parent span was not captured, often a trace cut by the window or by the
sampler), outside_measurement_window (the root or a span is not inside one window),
clock_inconsistency (a child starts before or ends after its parent, typically clock skew
between hosts or work that outlives its request), cycle.
The latency tables cover every span inside the windows; the call graph, critical path
and waterfalls cover eligible traces only.
