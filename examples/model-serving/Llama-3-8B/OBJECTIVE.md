# Objective — Llama-3-8B inference server

Serve Llama-3-8B on a single H100 with the best **throughput/latency trade-off**
under a realistic concurrent load, while keeping accuracy within the accuracy
checker's tolerance. Build an OpenAI-compatible `/v1/chat/completions` and
`/v1/completions` server.

This run is scored on a **Pareto frontier over two axes** (see `objectives.toml`),
both read from the benchmark's `--output-json` output:

- **`aggregate_throughput`** — output tokens/sec, **maximize**.
- **`p99_latency_ms`** — p99 end-to-end request latency in milliseconds, **minimize**.

A candidate is admitted to the frontier if it is non-dominated: at least as good
as the parent on both axes and strictly better on one. Raising throughput by
inflating tail latency (e.g. unbounded batch sizes) is a real trade-off, not a
free win — it moves you along the frontier, it does not dominate.

## Benchmark protocol — run EXACTLY this

Both axes are only comparable across candidates if every candidate is measured
under the **same fixed saturating load**. When you (the profiler) run the
benchmark, use these flags verbatim and change **only** `--url` (the live server)
and `--output-json` (the output path):

```
<benchmark_command> --url <SERVER_URL> --concurrency 16 --duration 20 --max-tokens 128 --temperature 0 --output-json <PATH>
```

- `--concurrency 16` drives a **closed-loop** load of exactly 16 in-flight
  requests, so `aggregate_throughput` measures true server capacity (not the
  arrival rate) and `p99_latency_ms` measures tail latency under contention.
- Do **not** use `--num-requests 1`, do not lower `--concurrency`, and do not
  shorten `--max-tokens`. A single-request or tiny workload makes throughput
  degenerate into first-token latency and gives the search no signal about
  batching or scheduling.
- The fixed 16-way load is also what bounds the frontier: no candidate can "win"
  the latency axis by serving fewer requests, because every candidate faces the
  identical load. A server that fails or starves most requests loses throughput
  and trips the benchmark-sanity / accuracy gates.

## Headline metric (`perf_metric`) and Pareto metrics — canonical fields, do not leave null

Headline metric: `aggregate_throughput` (output tok/s)

The scalar `perf_metric` (used for plateau detection and the scalar fallback) is
**`aggregate_throughput`** read directly from the benchmark JSON. In addition,
because this is a Pareto run, populate `ProfilerSummary.metrics` with **both**
objective values using these exact keys, read verbatim from the benchmark JSON:

```
metrics = {
  "aggregate_throughput": <benchmark JSON aggregate_throughput, float, tok/s>,
  "p99_latency_ms":       <benchmark JSON p99_latency_ms, float, ms>,
}
```

Also set `perf_unit = "tok/s"`. Read every value verbatim — do NOT derive,
invert, or substitute another field. Only set a metric to `null` if the server
never served a single successful request (the benchmark produced no data for
that field). Reporting `null` when a value was measured discards the run's
fitness and drops the candidate from the frontier.

## Notes

- Text-generation, dense causal LM. Hopper-class hardware assumed.
- Implement model layers explicitly (own attention / MLP / norm / RoPE); use
  `transformers` only as a utility for config / tokenizer / weight loading.
- Both the benchmark and the accuracy checker drive the **running server over
  HTTP** (no local model import). The accuracy checker enforces reference-free
  gates — sentinel-echo, known-answer, and greedy determinism — so a real
  prompt-conditioned forward pass is required; canned/echoed output fails.
