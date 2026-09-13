# Hotel Reservation correctness example

This input materializes the pinned DeathStarBench Hotel Reservation source and
uses an example-owned Go composition for accuracy and the shared Go
`servicebench` command for throughput. The candidate repository remains
unmodified.

```bash
vibesys --runs-dir /path/to/runs --local \
  --input examples/microservices/hotel-correctness
```

The accuracy gate starts the candidate with Docker Compose, waits for every
declared endpoint, runs the Hotel accuracy application, hard-restarts the
application containers for recovery checks, and removes containers and volumes
afterward. The benchmark uses the same workload and lifecycle, with
OpenTelemetry capture enabled for the measured run.

The example owns its workload and telemetry policy under `benchmark/` and its
Hotel request generator and semantic oracle under `evaluator/internal/hotel/`.
The shared evaluator supplies the generic runners, HTTP transport, and Hotel
benchmark adapter. `evaluator/go.mod` supports source-tree tests, while
`evaluator/runtime.mod` resolves the provisioned evaluator copy at
`_evaluator/microservice` during a run.
