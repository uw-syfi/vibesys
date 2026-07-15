# Microservices

**Use for:** optimizing microservice applications whose correctness and
performance are evaluated through service APIs such as HTTP, Thrift, gRPC, or
gateway/proxy routes.

**What this pack adds:**
- *Implementer:* focuses the agent on the end-to-end request path under test:
  client endpoint, gateway/proxy, downstream services, caches, databases,
  queues, service discovery, and load-balancing components. It also highlights
  common service-performance levers such as connection pooling, cache hit rates,
  database indexes, concurrency limits, serialization overhead, and gateway
  upstream tuning.
- *Judge:* adds always-on API compatibility checks, requires correctness before
  benchmarking, judges performance with the objective's headline metric plus
  success/error rates, and rejects microservice-specific reward hacks such as
  hard-coded responses, stale cache shortcuts, bypassed downstream state, or
  evaluator-owned file edits.
- *Orchestrator / Profiler:* suggest round-sized service optimization tasks and
  profile evidence for the benchmarked network path, including gateway logs,
  service logs, container/process usage, database/cache stats, and steady-state
  benchmark output.

Input bundles select it with `[agent].domain = "microservices"`. Task-specific
API contracts, ports, service startup steps, and workload mixes belong in the
input bundle's `OBJECTIVE.md` and related candidate contract files.
