# Objective — KV store (Redis RESP2)

Maximize **throughput (ops/sec)** on YCSB Workload A (50% read / 50% update,
Zipfian keys, single-client). Build a RESP2-compatible in-memory KV server.

## Notes

- Seed baseline: `reference/seed_server.py`, ~10k ops/sec on Workload A.
- Headline metric: `OVERALL.Throughput(ops/sec)` from YCSB output (higher is
  better). Secondary: p99 latency.
- YCSB Workload A stores each record as a **hash** (HSET / HGETALL), not plain
  GET/SET — optimize for that access pattern.
- Benchmark defaults to `--threads 1`; SO_REUSEPORT / multi-process helps
  aggregate throughput but not the single-client headline number.
