# Objective — KV store (Redis RESP2)

Maximize **throughput (ops/sec)** on YCSB Workload A (50% read / 50% update,
Zipfian keys) under **concurrent** client load. Build a RESP2-compatible
in-memory KV server.

## Notes

- Seed baseline: `reference/seed_server.py`, ~10k ops/sec.
- **Headline metric:** the benchmark's `PERF_METRIC:` line — median throughput
  over several fixed-duration runs at the default concurrency (`--threads 16`).
  Single-connection numbers are RTT-bound and hide the server's ceiling, so the
  headline is concurrent; use `--threads 1` only as a latency reference.
- **Latency SLA (a gate, not a footnote):** the throughput only counts if
  **p99 < 1.0 ms** for READ and UPDATE at the concurrent load. Winning ops/sec
  by blowing up p99 is a fail.
- Workload A stores each record as a **hash** (HSET / HGETALL), so optimize the
  hash path, and expect server-side **concurrency** (lock sharding, an efficient
  event loop, `SO_REUSEPORT`) to be the dominant lever once the network floor is
  in place.
