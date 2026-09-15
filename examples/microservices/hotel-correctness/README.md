# Hotel Reservation correctness example

Both tasks materialize the same pinned DeathStarBench Hotel Reservation source
and share the Go semantic oracle under `evaluator/` and workload under
`benchmark/`. Select the deployment explicitly:

```bash
vibesys --runs-dir /path/to/runs --local \
  --input examples/microservices/hotel-correctness --task compose

vibesys --runs-dir /path/to/runs --local --run-environment local --profiler none \
  --input examples/microservices/hotel-correctness --task kubernetes
```

This replaces the previous taskless Compose input. The Compose task retains its
Docker lifecycle and OpenTelemetry benchmark capture. See [Kubernetes setup](KUBERNETES.md)
for cluster prerequisites and lifecycle boundaries.

The accuracy application uses the packaged evaluator's generic Go runner and
HTTP transport. `evaluator/run.py` creates a temporary Go module file resolving
that exact package, without modifying the shared checker sources. The benchmark
uses the package's `servicebench` command. Source-tree Go tests continue to use
`evaluator/go.mod`.
