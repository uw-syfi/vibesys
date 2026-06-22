# KV store (Redis RESP2)

**Use for:** building a non-persistent, in-memory key-value store that speaks the
Redis RESP2 protocol — benchmarked by YCSB for throughput and checked
byte-for-byte against a Redis oracle. Pair with `--modality kv_store` and
`--backend cpu`:

```
vibe-serve --outer-loop agent --modality kv_store --backend cpu \
  --interface service --domain examples/kv-store/kv-store.md ...
```

`--interface service` is the right fit here: the store is only ever exercised
over its RESP2 socket (the checker and YCSB never import the candidate's code),
so the agent is free to implement it in any language and the harness starts it
via `./run.sh <port>`. Drop the flag (default `inprocess`) to pin the
implementation to a Python `main.py` instead.

**What this pack adds** (the cross-cutting "what good means" for this problem
space — the per-task RESP2 I/O contract lives in the `kv_store` modality):

- *Implementer:* systems-engineering framing, the optimization space, and the
  build constraints.
- *Judge:* correctness-before-performance gating and the KV-store reward-hack
  semantics (a server that returns canned responses without touching its data
  structure).
- *Orchestrator:* the baseline optimization floor the planner seeds the roadmap
  from (uvloop, pipelining, TCP_NODELAY) before chasing workload-specific wins.

Author your own by copying this file. See
`src/vibe_serve/loops/agent/templates/_domain/README.md` for the format.

## implementer
You are a systems engineer building a high-performance in-memory key-value store. Favor measured wins over speculation — profile before and after each change.

## Optimization space

- Concurrency: an optimized event loop / async I/O (e.g. asyncio+uvloop, epoll, io_uring), threading, multiprocessing, SO_REUSEPORT
- Data structures: a fast built-in hash map, open-addressing hash table, skip list
- Protocol parsing: zero-copy buffers, vectored I/O, a hand-rolled RESP scanner
- Memory: object pooling, slab allocation, pre-allocated buffers
- Networking: TCP_NODELAY, pipelining, batched writes
- Hot-path acceleration: native code / FFI (C, Cython, Rust, …)

## Constraints

- Non-persistent (in-memory only, no durability) and single-node (no replication).
{% if interface | default("inprocess") == "service" %}
- Implementation language is yours to choose — you are judged only over the RESP2 wire. The reference implementation is written in Python, but it is only the correctness oracle: your implementation does **not** have to be in Python. Pick whatever language and runtime best fit a CPU-bound network server.
{% else %}
- Implementation language is Python; C extensions are permitted for hot paths.
{% endif %}
- The accuracy checker must pass before performance is measured.

## judge
## Performance gate — correctness first

Performance is only meaningful on an implementation that passes the accuracy checker. Treat any correctness failure as an automatic round **fail** regardless of throughput, and surface the failing operations in `feedback`.

## Reward-hack detection (proactive — do this even when all checks pass)

For a KV store, reward-hacking means the server satisfies the checker's fixed operation sequence **without actually storing to / retrieving from a real data structure** — e.g. canned or templated responses, a replay of the checker's expected outputs, or a constant reply per command type.

Verify the store is real: after different `SET`s to the same key, a `GET` must return the most recent value (not a constant), and a freshly chosen key/value pair the checker never used must round-trip correctly. A server that passes the deterministic checker sequence but cannot round-trip a novel key is reward-hacking — **verdict is fail**, and name the offending function / branch in `feedback`.

## orchestrator
## Optimization priority (read before choosing the next task)

A CPU-bound RESP2 network server has a well-established **optimization floor** — three wins that address the dominant cost sources (event loop, syscall count, request-response latency) before any workload-specific tuning. On a fresh run, seed the roadmap from these; confirm all three are in place before proposing exotic optimizations:

1. **A fast event loop** — {% if interface | default("inprocess") == "service" %}use the most efficient async I/O facility the chosen runtime offers (e.g. uvloop for Python, epoll/io_uring for native servers, the runtime's native scheduler for Go/Rust){% else %}uvloop — a drop-in 2-4x asyncio event-loop speedup{% endif %}.
2. **Pipelining** — parse all commands available per `recv`, batch their replies into one `send`.
3. **TCP_NODELAY** — disable Nagle's algorithm so small request-response packets aren't delayed.

Only after these three are in place should you spend rounds on advanced work (custom RESP parser in native code, open-addressing hash table, slab allocation, `SO_REUSEPORT` multi-process). This is a single-client YCSB headline metric, so multi-process scaling helps aggregate throughput but not the single-client number — weigh tasks against the objective's metric.

There is **no GPU, no model, and no tensor work** in this target; ignore any instinct to reach for batching/attention/CUDA-graph techniques.
{% if interface | default("inprocess") == "service" %}
## Implementation language — prefer a compiled language

This target is judged **only over the RESP2 wire** and is CPU-bound, where a compiled systems language (C, Rust, or Go) has a decisive throughput advantage over an interpreted one. Prefer building the baseline **directly in a compiled language** rather than iterating on the Python seed: `reference/seed_server.py` is the correctness oracle only, not the starting codebase. Don't defer the language choice to a later round — the baseline itself is the compiled server, invoked via `run.sh <port>`.
{% endif %}
## Task examples

Tasks should be comparable in scope to, e.g.:
{% if interface | default("inprocess") == "service" %}
- "Build a native (C / Rust / Go) RESP2 server binary invoked via `run.sh <port>`."
- "Replace line parsing with a hand-rolled zero-copy RESP scanner."
- "Switch the event loop to epoll / io_uring (uvloop if staying on Python)."
{% else %}
- "Switch the asyncio server to uvloop with a safe fallback."
- "Add a hand-rolled RESP scanner over a per-connection bytearray."
{% endif %}
- "Parse all complete commands per `recv` and batch their replies into one `send` (pipelining)."
- "Set `TCP_NODELAY` on accepted sockets."
- "Swap the hash map for an open-addressing table sized to the keyspace."

## roadmap_seed
{% if interface | default("inprocess") == "service" %}
- **M1 Native compiled RESP2 baseline** — `in_progress`. Why: this CPU-bound wire target is judged only over RESP2; a compiled systems language (C / Rust / Go) removes the interpreter overhead that dominates a Python server. Build the baseline directly in the compiled language — the Python seed is the correctness oracle only.
{% else %}
- **M1 RESP2 optimization floor on the Python seed (uvloop)** — `in_progress`. Why: a fast event loop addresses the dominant single-client network/syscall cost of the seed asyncio server.
{% endif %}
- **M2 Pipelining + TCP_NODELAY** — `todo`. Why: parse all complete commands per `recv`, batch replies into one `send`, and disable Nagle so small request/response packets aren't delayed.
- **M3 Hot-path data structures & parsing** — `todo`. Why: an open-addressing hash table and a hand-rolled zero-copy RESP scanner cut per-op overhead once the network floor is in place.
