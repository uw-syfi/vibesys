# KV store (Redis RESP2)

**Use for:** a non-persistent, in-memory key-value store that speaks Redis RESP2
— benchmarked by YCSB for throughput and checked byte-for-byte against a Redis
oracle. Pair with `--modality kv_store --backend cpu`:

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
You are a systems engineer building a high-performance in-memory key-value store. Favor measured wins over speculation — profile before and after each change, and let the profile decide what to optimize next.

## Constraints

- Non-persistent (in-memory only) and single-node (no replication).
- `reference/seed_server.py` is a reference baseline, not a required starting point.

## judge
## Two gates: correctness, then the latency SLA

Performance is meaningless until the accuracy checker passes — treat any correctness failure as an automatic round **fail** regardless of throughput, and name the failing operations in `feedback`. The OBJECTIVE's p99 SLA is the second gate: if READ or UPDATE p99 exceeds the bound at the concurrent load, the round **fails** even if throughput improved. Name the regression.

## Reading the performance number (do not eyeball it)

Set `perf_metric` (`perf_unit` = `ops/sec`) from the benchmark's **`PERF_METRIC: <n> ops/sec`** line — the median of several fixed-duration runs. Do **not** read it from the human-readable text, a per-phase line, or `operationcount` (a common mistake is reporting the op count `100000` as throughput). If `PERF_COV:` (run-to-run spread) is high (e.g. > 10%), say so in `analysis` and treat small round-over-round deltas as noise.

## Reward-hack detection (do this even when all checks pass)

Reward-hacking here means the server satisfies the checker's fixed sequence **without a real data structure** — canned/templated responses, a replay of expected outputs, or a constant reply per command. Verify the store is real: after different `SET`s to one key a `GET` must return the most recent value, and a fresh key/value the checker never used must round-trip. A server that passes the deterministic sequence but cannot round-trip a novel key is reward-hacking — **verdict is fail**; name the offending function / branch in `feedback`.

## orchestrator
This is a CPU-bound network server — there is no GPU, model, or tensor work. The headline metric is **concurrent** throughput (YCSB `--threads 16`) under a p99 latency SLA. Scope each round from what the current profile shows is the dominant bottleneck rather than a fixed technique checklist, and confirm each change holds the SLA — a throughput win that violates the p99 SLA is a fail.
{% if interface | default("inprocess") == "service" %}
This target is judged only over the wire and is CPU-bound, where a compiled systems language (C / Rust / Go) has a decisive edge over an interpreter. Prefer building the baseline **directly in a compiled language** rather than iterating on the Python seed — don't defer the language choice to a later round.
{% endif %}
## Task examples

Tasks should be comparable in scope to, e.g.:

- "Stand up a correct RESP2 baseline the checker and benchmark accept end to end."
- "Profile the server under concurrent load and record where wall-clock time goes."
- "Fix the single dominant hotspot the last profile named, measured before and after."

## roadmap_seed
{% if interface | default("inprocess") == "service" %}
- **M1 Working RESP2 baseline** — `in_progress`. Why: get a correct server the checker and benchmark accept end to end; a compiled language (C / Rust / Go) suits this CPU-bound wire target, so build the baseline directly in it — the Python seed is a reference only.
{% else %}
- **M1 Working RESP2 baseline (Python `main.py`)** — `in_progress`. Why: get a correct server the checker and benchmark accept end to end, starting from the seed.
{% endif %}
- **M2 Profile under concurrent load** — `todo`. Why: measure where wall-clock time goes at the concurrent headline load before optimizing, so later rounds target a real bottleneck rather than a guess.
- **M3 Address the dominant bottleneck** — `todo`. Why: apply the highest-leverage fix the profile points to, measured before and after, without regressing the p99 SLA.
