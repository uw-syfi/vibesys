# KV store (Redis RESP2)

**Use for:** a non-persistent, in-memory key-value store that speaks Redis RESP2
— benchmarked by YCSB for throughput and checked for **linearizability** under
concurrent load (Porcupine). Pair with `--modality kv_store --backend cpu`:

```
vibe-serve --outer-loop agent --modality kv_store --backend cpu \
  --interface service --domain examples/kv-store/kv-store.md ...
```

Under `--interface service` the store is exercised only over its RESP2 socket
(the checker and YCSB never import the candidate), so the agent picks the
language and the harness starts it via `./run.sh <port>`. Drop the flag
(default `inprocess`) to pin the implementation to a Python `main.py`.

This pack carries the cross-cutting "what good means"; the per-task RESP2 I/O
contract lives in the `kv_store` modality. Author your own by copying this file
— see `src/vibe_serve/loops/agent/templates/_domain/README.md` for the format.

## implementer
You are a systems engineer building a high-performance in-memory key-value store. Favor measured wins over speculation — profile before and after each change.

## Optimization space

- Concurrency: an efficient event loop / async I/O (asyncio+uvloop, epoll, io_uring), threading, multiprocessing, SO_REUSEPORT, and — critically under concurrent load — sharded / striped locks or per-core state to kill a single global-lock bottleneck
- Data structures: a fast built-in hash map, open-addressing hash table, skip list
- Protocol parsing: zero-copy buffers, vectored I/O, a hand-rolled RESP scanner
- Memory: object pooling, slab allocation, pre-allocated buffers
- Networking: TCP_NODELAY, pipelining, batched writes
- Hot-path acceleration: native code / FFI (C, Cython, Rust, …)

## Constraints

- Non-persistent (in-memory only) and single-node (no replication).
- `reference/seed_server.py` is a reference baseline, not a required starting point.

## judge
## Two gates: correctness, then the latency SLA

Performance is meaningless until the accuracy checker passes — treat any correctness failure as an automatic round **fail** regardless of throughput, and name the failing operations in `feedback`. The OBJECTIVE's p99 SLA is the second gate: if READ or UPDATE p99 exceeds the bound at the concurrent load, the round **fails** even if throughput improved. Name the regression.

## Reading the performance number (do not eyeball it)

Set `perf_metric` (`perf_unit` = `ops/sec`) from the benchmark's **`PERF_METRIC: <n> ops/sec`** line — the median of several fixed-duration runs. Do **not** read it from the human-readable text, a per-phase line, or `operationcount` (a common mistake is reporting the op count `100000` as throughput). If `PERF_COV:` (run-to-run spread) is high (e.g. > 10%), say so in `analysis` and treat small round-over-round deltas as noise.

## Reward-hack detection (do this even when all checks pass)

The linearizability checker drives **random** values the server can't precompute, so canned/replay/constant-reply stores fail it outright — a `GET` must return the exact value last `SET` on that key, under concurrency. The residual risk is a server that special-cases the **benchmark's** access pattern (YCSB's key format or field names) instead of implementing a general store. Verify generality: a fresh key/value outside the benchmark's pattern must round-trip, and throughput must not depend on hard-coded knowledge of the workload. If it does — **verdict is fail**; name the offending function / branch in `feedback`.

## orchestrator
## Optimization priority (read before choosing the next task)

Establish the CPU-bound network **floor** before any exotic work, and confirm all three are in place first:

1. **A fast event loop** — {% if interface | default("inprocess") == "service" %}the most efficient async I/O the chosen runtime offers (uvloop for Python; epoll/io_uring or the native scheduler for Go/Rust){% else %}uvloop — a drop-in 2-4x asyncio speedup{% endif %}.
2. **Pipelining** — parse every command available per `recv`, batch their replies into one `send`.
3. **TCP_NODELAY** — disable Nagle so small request/response packets aren't delayed.

The headline is **concurrent** (YCSB `--threads 16`), so the dominant post-floor bottleneck is **contention, not per-op CPU**: a single global lock serializes every request. After the floor, prioritize **lock sharding / per-core state**, then an efficient multi-connection event loop (thread pool sized to cores, or epoll / io_uring), then `SO_REUSEPORT` / multi-process scaling. Custom RESP parsing, open-addressing tables, and slab allocation are secondary until the server is no longer contention-bound. A throughput win that violates the p99 SLA is a fail.

This is a CPU-bound network server — there is no GPU, model, or tensor work.
{% if interface | default("inprocess") == "service" %}
This target is judged only over the wire and is CPU-bound, where a compiled systems language (C / Rust / Go) has a decisive edge over an interpreter. Prefer building the baseline **directly in a compiled language** rather than iterating on the Python seed — don't defer the language choice to a later round.
{% endif %}
## Task examples

Tasks should be comparable in scope to, e.g.:
{% if interface | default("inprocess") == "service" %}
- "Build a native (C / Rust / Go) RESP2 server binary invoked via `run.sh <port>`."
- "Switch the event loop to epoll / io_uring (uvloop if staying on Python)."
{% else %}
- "Switch the asyncio server to uvloop with a safe fallback."
{% endif %}
- "Parse all complete commands per `recv` and batch their replies into one `send` (pipelining)."
- "Set `TCP_NODELAY` on accepted sockets."
- "Shard the store across N independent lock+map stripes keyed by hash to kill the global-lock bottleneck."
- "Swap the hash map for an open-addressing table sized to the keyspace."

## roadmap_seed
{% if interface | default("inprocess") == "service" %}
- **M1 Native compiled RESP2 baseline** — `in_progress`. Why: this CPU-bound wire target is judged only over RESP2; a compiled language (C / Rust / Go) removes the interpreter overhead that dominates a Python server. Build the baseline directly in it — the Python seed is a reference only.
{% else %}
- **M1 RESP2 optimization floor on the Python seed (uvloop)** — `in_progress`. Why: a fast event loop addresses the dominant network/syscall cost of the seed asyncio server.
{% endif %}
- **M2 Pipelining + TCP_NODELAY** — `todo`. Why: parse all complete commands per `recv`, batch replies into one `send`, and disable Nagle.
- **M3 Concurrency scaling & lock sharding** — `todo`. Why: the headline is concurrent, so a single global lock is the dominant ceiling once the network floor is in place. Shard the keyspace across independent lock+map stripes (or go per-core / lock-free) and feed many connections efficiently. Guard the p99 SLA.
- **M4 Hot-path data structures & parsing** — `todo`. Why: an open-addressing hash table and a hand-rolled zero-copy RESP scanner cut per-op overhead once contention is removed.
