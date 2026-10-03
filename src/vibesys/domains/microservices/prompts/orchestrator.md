## Microservice planning guidance

Choose round-sized tasks that improve the measured service path while keeping
the public API stable.

Good task shapes include:

- Establish a baseline by running the checker and benchmark, then identify the
  slowest endpoint or dependency from logs, timing headers, or profiler output.
- Add or tune client connection pooling for a hot downstream service.
- Tune gateway/proxy upstream pools, keep-alive, buffering, or routing for the
  benchmarked endpoints.
- Improve cache usage with explicit invalidation or consistency constraints.
- Add a database index or query rewrite for the workload's hot read path.
- Reduce serialization, JSON encoding, Thrift/gRPC conversion, or repeated
  per-request setup on the hot path.
- Tune worker counts, queue sizes, runtime settings, and timeouts for the
  offered load.

Write pass criteria in terms of the objective's headline metric and correctness
gate. Include acceptable error-rate or success-rate constraints when the
benchmark reports them. Avoid criteria that reward isolated handler timing while
the end-to-end service gets worse.

Prefer one change per round. If a service lifecycle step is uncertain, first ask
the implementer to document and validate startup, health, checker, and benchmark
commands before attempting optimization.
