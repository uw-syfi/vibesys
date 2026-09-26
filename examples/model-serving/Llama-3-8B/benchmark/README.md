# Benchmark

Request Factory owns synthetic token generation, `/v1/completions` transport,
request timing, and per-point summaries. The bundle wrapper runs a closed-loop
concurrency sweep and selects throughput and p99 latency from the same
operating point. The pinned RF summary currently reports p50/p90 end-to-end
latency only, so p99 is calculated from RF's per-request `total_duration_ms`
records rather than approximated from a lower percentile. Each point builds a
deterministic temporary text corpus and sets RF's token-pool limit to the
larger of twice the prompt length or the request count. An RF warning that the
token pool is too short fails the point.

Run the server on the same host as the benchmark and use loopback. Keep the
server process alive for the whole sweep. The default trace has 512 independent
requests per point, 256 input tokens, and 128 output tokens. Every point uses
RF's saturated arrival mode bounded by the selected concurrency. This replaces
the previous fixed-duration client run with a fixed-volume RF trace; retain this
methodology change when comparing results across the migration.

The default coarse sweep uses concurrency `1,2,4,8,16,32,64,128`. The wrapper
retains every point, flags failures, a throughput falloff beyond 5%, or a p99
latency increase over 2x without a throughput gain beyond 3% as a suspected
overload boundary, and confirms adjacent points with repeat runs plus an
intermediate concurrency point. If throughput is still rising by more than 3%
at the highest requested concurrency, the run fails instead of claiming a peak.
The final `aggregate_throughput` and `p99_latency_ms` both come from the
highest-throughput sustainable point. `--concurrencies` can supply another
ascending comma-separated sweep; `--request-count`, `--input-tokens`, and
`--output-tokens` control the trace shape. For CPU-only contract testing, run
the shared fake-server smoke with a local Request Factory engine:

```bash
uv run python -m tests.examples.request_factory_cpu_smoke \
  --profile examples/model-serving/Llama-3-8B/benchmark/cpu_smoke.toml \
  --request-factory-engine <RF_ENGINE>
```
