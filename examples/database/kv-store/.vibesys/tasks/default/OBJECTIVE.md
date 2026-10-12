# Objective — KV store (Redis RESP2)

Maximize **throughput (ops/sec)** on YCSB Workload A (50% read / 50% update,
Zipfian keys) under **concurrent** client load. Build a RESP2-compatible
in-memory KV server.

## Notes

- Seed baseline: `.vibesys/tasks/default/reference/seed_server.py`, ~10k ops/sec.
- **Headline metric:** the benchmark's `PERF_METRIC:` line — median throughput
  over several fixed-duration runs at the default concurrency (`--threads 16`).
  Single-connection numbers are RTT-bound and hide the server's ceiling, so the
  headline is concurrent; use `--threads 1` only as a latency reference.
- **Latency SLA (a gate, not a footnote):** the throughput only counts if
  **p99 < 1.0 ms** for READ and UPDATE at the concurrent load. Winning ops/sec
  by blowing up p99 is a fail.
- Workload A stores each record as a **hash** (HSET / HGETALL) and drives
  concurrent client load (`--threads 16`), so the server's behaviour under
  contention is what the headline measures.

## Agent guidance

- CPU-bound network server — no GPU, model, or tensor work. Scope each round
  from what the profile shows is the dominant bottleneck.
- Judged only over the wire (`--interface service`). A compiled systems language
  (C / Rust / Go) has a decisive edge over an interpreter; prefer building the
  baseline directly in a compiled language rather than iterating on the Python
  seed.
- Non-persistent (in-memory only) and single-node (no replication).
- The candidate must listen on a TCP port and be started via `./run.sh <port>`.

## Wire contract and acceptance rules

This is a TCP server speaking the Redis RESP2 wire protocol, not an HTTP
server. There is no `/health` endpoint; check liveness with
`redis-cli -p <port> PING` (expect `PONG`).

- Support GET, SET, DEL, HSET, HMSET, HGETALL, PING, FLUSHDB, DBSIZE, COMMAND,
  HELLO and CLIENT. `HELLO` returns `+OK\r\n` (signals RESP2-only to clients).
  Do not fail an implementation for missing commands outside this set.
- Responses must be valid RESP2 (type prefix, CRLF, exact bulk-string
  lengths). A malformed response is an immediate failure.
- The server must answer `PING` within 5 seconds of being started. Ship a
  `./run.sh <port>` launcher.
- In-memory only: do not fail an implementation for lacking durability.
- The accuracy checker (`checker.py --port <port>`) drives the same
  deterministic sequence, sequential and concurrent, against the candidate and
  a Redis oracle (`redis-py`, `protocol=2`). Every response must match
  byte-for-byte. Any correctness failure fails the round regardless of
  throughput; name the failing operations.
- The benchmark (`benchmark.py --port <port>`) needs Java 8+ and downloads
  YCSB 0.17.0 (Redis binding) to `~/.cache/vibesys/kv-store/ycsb/` on first
  run (override with `KV_STORE_YCSB_HOME`).
- Set `perf_metric` (`perf_unit` = `ops/sec`) from the benchmark's
  `PERF_METRIC: <n> ops/sec` line, never from per-phase text or the operation
  count. If `PERF_COV:` exceeds about 10%, treat small round-over-round deltas
  as noise.
- Reward hacking: the store must be real. After different `SET`s to one key a
  `GET` returns the latest value, and a fresh key the checker never used
  round-trips. Canned or replayed responses fail the round; name the
  offending function or branch.

## Profiling

Start the server with `./run.sh 6380 &`, find its PID with
`lsof -ti tcp:6380 -s tcp:LISTEN`, drive load with the benchmark command and
`--port 6380`, then sample with `py-spy record` (Python), `perf record -g`
(native) or `strace -c -e trace=read,write,epoll_wait`. Key axes are ops/sec,
p99 latency and per-core CPU; let the largest consumer in the profile pick the
next optimization.
