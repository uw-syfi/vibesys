# stitch-analysis

Distributed-trace analysis of a Stitch graph and its bounded text rendering:
latency tables, the aggregated call graph per root endpoint with client/server
hops, the `wall_clock_active_leaf_v1` critical path, and representative
traces. It reads only `stitch-core` types: span semantics (kind, operation,
failure, datastore) come from the importer that built the graph.

The trace graph and critical path port `trace_graph.go` and `critical_path.go`
from the evaluator's telemetry package. `stitch-cli/tests/render.rs` checks the
port against numbers the Go code produced for a shared fixture; change both
implementations together.
