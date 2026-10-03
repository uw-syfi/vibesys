You are working on a **microservice system**. Optimize the deployed service path
while preserving the API and data semantics that the objective, checker, and
benchmark exercise.

## Development priorities

1. Read the objective, manifest, README, and any candidate contract files before
   editing code or deployment configuration.
2. Identify the request path under test: client endpoint, gateway/proxy,
   downstream services, caches, databases, queues, and any service discovery or
   load-balancing components.
3. Run the accuracy checker before relying on benchmark numbers. Performance
   wins are invalid if response shape, status codes, ordering, visibility,
   consistency, pagination, or error semantics regress.
4. Treat benchmark and checker code as evaluator-owned unless the objective
   explicitly says otherwise. Do not edit them to make a candidate pass.
5. Keep changes scoped to the candidate service, deployment, or configuration
   surface required by the task.

## Common optimization levers

Prefer changes that improve the end-to-end workload named by the objective:

- Reduce avoidable network hops, serialization overhead, and connection setup.
- Reuse clients and connection pools instead of reconnecting per request.
- Tune concurrency limits, worker pools, queue sizes, timeouts, and runtime
  settings for the measured load.
- Improve cache hit rates while preserving invalidation and visibility rules.
- Add or adjust database indexes only when they match the benchmarked queries.
- Move repeated pure computation out of hot request paths.
- Tune gateway/proxy routing, keep-alive, buffering, and upstream pools when the
  gateway is on the critical path.

Avoid shortcuts that only satisfy the benchmark shape: hard-coded responses,
skipping required downstream state checks, returning stale data after writes, or
changing public API behavior.
