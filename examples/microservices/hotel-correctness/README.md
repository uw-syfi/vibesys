# Hotel Reservation correctness example

This input runs the Hotel Reservation candidate from the pinned DeathStarBench
source declared in `vibesys.input.toml`. It invokes the `hotel-correctness`
entrypoint from the versioned `vibesys-evaluator-microservice` package, so the
generator, oracle, and checker remain evaluator-owned and the candidate stays
unmodified.

Custom isolated run-environment images must provide Python 3.12 or newer and
the `vs_correctness` package used by the evaluator.

```bash
vibesys --runs-dir /path/to/runs --local \
  --input examples/microservices/hotel-correctness
```

The Python suite in
`resources/evaluators/microservice/hotelcorrectness/hotel_suite.py` generates
seed-replayable histories for authentication, recommendations, search, and
reservation capacity and date isolation. Its checker owns Compose lifecycle,
multi-service readiness, cleanup, topology observation, and reporting. Its
Python model and oracle check the exact catalog, malformed requests, all seeded
users, optional reservation quantities, atomic rejected writes, hotel and date
isolation, invalid-auth state behavior, persistent HTTP, and crash recovery.
The configured `--cases 4` adds four randomized model histories to four fixed
contract cases. Each case starts from fresh service state, so startup time scales
with this value; the accuracy timeout allows for all eight isolated starts.
See the [`vs-correctness` README](../../../libs/vs-correctness/README.md) for
framework APIs, replay, shrinking, and report semantics.
