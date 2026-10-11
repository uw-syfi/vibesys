# Objective — Llama-3-8B inference server

Serve Llama-3-8B on a single H100 with the best **throughput/latency trade-off**
under a realistic concurrent load, while keeping accuracy within the accuracy
checker's tolerance. Build an OpenAI-compatible `/v1/chat/completions` and
`/v1/completions` server.

This run is scored on a **Pareto frontier over two axes** (see `objectives.toml`),
both emitted by the benchmark's VibeSys result protocol:

- **`aggregate_throughput`** — output tokens/sec, **maximize**.
- **`p99_latency_ms`** — p99 end-to-end request latency in milliseconds, **minimize**.

A candidate is non-dominated when no retained point is at least as good on both
axes and strictly better on one. It need not dominate its immediate parent.
Raising throughput by inflating tail latency (e.g. larger batch sizes) is a real
trade-off, not a free win: a credible bounded result can move along the frontier
without replacing the lower-latency parent.

## Benchmark protocol — Request Factory concurrency sweep

The canonical score is the highest sustainable output-token throughput reached
before the server becomes overloaded. Request Factory drives independent
synthetic token requests through `/v1/completions` using a saturated,
concurrency-bounded trace. The default shape is 512 requests per point, 256
input tokens, and 128 output tokens. The methodology changes from fixed-duration
bundle-generated prompts to a fixed-volume RF trace; comparisons to historical
scores should account for that change.

Wait for the server health endpoint before the sweep and keep the same server
process alive across every point. The trace uses 512 requests per point, 256
input tokens, and 128 output tokens. RF sends independent requests in saturated
arrival mode, bounded by the point's concurrency. This fixed-volume workload
replaces the former duration-limited bundle client; its scores should not be
treated as directly comparable with older fixed-duration runs.
Each coarse, midpoint, and confirmation invocation uses deterministic but
distinct prompt content, so keeping one server alive does not turn later sweep
points into prefix-cache replays of earlier points.

Run the benchmark client on the same host as the server and send requests over
loopback. This keeps external routing out of the measurements while exercising
the OpenAI-compatible HTTP/SSE serving path.

The default coarse sweep is concurrency `1,2,4,8,16,32,64,128`. The wrapper
flags failures, throughput below 95% of the best earlier point, or throughput
within 3% of the best accompanied by p99 end-to-end latency above 2x the last
sustainable point. It confirms an overload bracket with an intermediate point
and repeats both adjacent points. Without a detected overloaded point, the
highest load must be within 3% of an earlier throughput peak or the benchmark
fails rather than report an unestablished peak.

Retain and report every sweep row. The canonical `aggregate_throughput` and
`p99_latency_ms` are both selected from the same highest-throughput sustainable
point. RF's pinned aggregate summary has no p99 field, so the wrapper derives
p99 from RF's per-request total-duration records. The RF workload count and
token lengths are checked against each summary and request log.
The protocol-v2 result stream contains only the selected metric row. The full
sweep, repetitions, and selected concurrency remain in `--output-json`.

## Headline metric (`perf_metric`) and Pareto metrics — canonical fields, do not leave null

Headline metric: `aggregate_throughput` (output tok/s)

The scalar `perf_metric` is the peak sustainable **`aggregate_throughput`**.
Populate `ProfilerSummary.metrics` with **both** objective values using these
exact keys from the selected operating point:

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

For a targeted, directly comparable end-to-end point that is not a full
canonical sweep, keep `perf_metric` and canonical `metrics` empty. The agent
loop may instead record the same two values as provisional `candidate_metrics`,
with the raw artifact and operating point, so a reviewed trade-off remains an
alternate parent without being mistaken for the official score.

## Notes

- Text-generation, dense causal LM. Hopper-class hardware assumed.
- Implement model layers explicitly (own attention / MLP / norm / RoPE); use
  `transformers` only as a utility for config / tokenizer / weight loading.
- Both the benchmark and the accuracy checker drive the **running server over
  HTTP** (no local model import). The accuracy checker enforces reference-free
  gates — sentinel-echo, known-answer, and greedy determinism — so a real
  prompt-conditioned forward pass is required; canned/echoed output fails.
