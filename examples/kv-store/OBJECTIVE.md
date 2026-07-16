# Objective — KV store (Redis RESP2)

Serve YCSB Workload A (50% read / 50% update, Zipfian keys) under **concurrent**
client load as efficiently as possible. Build a RESP2-compatible in-memory KV
server. Do **less server work per operation** — including on the read and update
paths specifically, not just generic transport.

## Notes

- Seed baseline: `reference/seed_server.py`, ~10k ops/sec.
- **Scored metric:** `ops_per_cpu_sec` — operations served per second of server
  CPU (its `PERF_CPU_PER_OP:` line is the inverse, µs/op). It is measured
  *externally* from the server's `/proc` CPU over the run, so — unlike raw
  throughput — it is immune to the YCSB client saturating before the server does,
  and it rewards a genuine per-op efficiency win (e.g. a cheaper read path) even
  when transport-bound throughput is flat. Higher is better.
- **Throughput and latency are gates, not the score.** Report and preserve
  `throughput_ops_per_sec` (median over several fixed-duration runs) and keep
  **p99 < 1.0 ms** for READ and UPDATE at the concurrent load. Winning efficiency
  by collapsing throughput or blowing up p99 is a fail. Single-connection numbers
  are RTT-bound and hide the server; `--threads 1` is a latency reference only.
- The benchmark drives the server from several independent client JVMs
  (`--client-procs`) to reach server saturation, reports `server_cpu_cores` as a
  saturation check, and can isolate per-op-type server cost (`--probe-per-op`) so
  a read- or update-specific optimization is attributable. Record shape is tunable
  (`--field-count`/`--field-length`); Workload A stores each record as a **hash**
  (HSET / HGETALL).

## Agent guidance

- CPU-bound network server — no GPU, model, or tensor work. Scope each round
  from what the profile shows is the dominant bottleneck.
- Judged only over the wire (`--interface service`). A compiled systems language
  (C / Rust / Go) has a decisive edge over an interpreter; prefer building the
  baseline directly in a compiled language rather than iterating on the Python
  seed.
- Non-persistent (in-memory only) and single-node (no replication).
- The candidate must listen on a TCP port and be started via `./run.sh <port>`.
