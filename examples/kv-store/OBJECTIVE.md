# Objective — KV store (Redis RESP2)

Serve YCSB Workload A (50% read / 50% update, Zipfian keys) under **concurrent**
client load as efficiently as possible. Build a RESP2-compatible in-memory KV
server. Do **less server work per operation** — including on the read and update
paths specifically, not just generic transport.

The normative service, RESP2, concurrency, and lifecycle requirements are in
[`CANDIDATE_CONTRACT.md`](CANDIDATE_CONTRACT.md).

## Notes

- Seed baseline: `reference/seed_server.py`, ~10k ops/sec.
- **Headline metric: `ops_per_cpu_sec` (maximize).** This is operations served per second of server
  CPU (its `PERF_CPU_PER_OP:` line is the inverse, µs/op). It is measured
  *externally* from the stable candidate process group's Linux `/proc` CPU over
  the run, so — unlike raw
  throughput — it is immune to the YCSB client saturating before the server does,
  and it rewards a genuine per-op efficiency win (e.g. a cheaper read path) even
  when transport-bound throughput is flat. Higher is better.
- **Throughput and latency are enforced gates, not the score.** Preserve at
  least 10,000 `throughput_ops_per_sec` (median over fixed-duration runs) and keep
  **p99 < 1.0 ms** for READ and UPDATE at the concurrent load. Winning efficiency
  by collapsing throughput or blowing up p99 makes the benchmark exit nonzero.
  Single-connection numbers are RTT-bound and hide the server; `--threads 1` is
  a latency reference only.
- The benchmark drives the server from several independent client JVMs
  (`--client-procs`) and validates the load plateau with a higher-client-count
  probe. `server_cpu_cores` remains a diagnostic. Optional non-scoring
  diagnostics (`--probe-per-op`, `--field-count`/`--field-length`) can attribute
  per-op CPU or enlarge records; Workload A stores each record as a **hash**
  (HSET / HGETALL).

## Agent guidance

- CPU-bound network server — no GPU, model, or tensor work. Scope each round
  from benchmark CPU/per-op evidence and an actual profile when profiling is
  available.
- Judged only over the wire (`--interface service`). A compiled systems language
  (C / Rust / Go) has a decisive edge over an interpreter; prefer building the
  baseline directly in a compiled language rather than iterating on the Python
  seed.
- Non-persistent (in-memory only) and single-node (no replication).
- The candidate must listen on a TCP port and be started via `./run.sh <port>`.
- Scored evaluation is Linux-only because trusted CPU accounting requires
  procfs. Generic Linux `--profiler auto` currently disables the separate
  profiler phase; `perf` may still be used manually when permitted.
