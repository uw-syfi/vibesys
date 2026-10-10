# Benchmark

Run `uv run --offline --no-dev python .vibesys/tasks/shared-prefix/benchmark/benchmark.py --help`
from the example root. The model must be the pinned local snapshot. The benchmark
owns a fresh server and excludes startup from request latency while retaining
startup/warmup in server process peak RSS. One warmup precedes four concurrent
scored requests sharing the document. All answers and streams must pass.

- `p50_ttft_ms`: median dispatch to first nonempty content delta, including
  queueing, prefill, first decode, and transport. For four samples, average the
  middle two values.
- `p50_e2e_ms`: median dispatch to terminal completed stream.
- `output_throughput_tok_per_sec`: sum of actual completion tokens divided by
  scored batch makespan, from earliest dispatch to latest stream completion.
  SSE chunks are never counted as tokens.
- `server_peak_rss_bytes`: high-water RSS of the serving process, collected
  externally. It is not Metal allocator peak or total unified memory.
- `p50_prompt_processing_ms` and `metal_peak_memory_bytes`: optional measured
  telemetry, absent from stock HTTP. Unavailable reasons are saved separately.

`--vs-output PATH` writes evaluator protocol-v2 JSONL. The required `hello` is
flushed before measurement and followed by one finite metric `result` or an
`error`. Unavailable optional metrics are omitted; numeric protocol fields never
contain null. Primary minimization is selected by `objectives.toml`.

`--artifact-dir` must be new and the protocol file must be outside that directory.
For manual baseline measurements, specify `--round 0 --status baseline`.
Unassigned framework invocations need an authoritative round mapping at report
time. Failed warmup or invalid results never receive a performance score.
Changing cache behavior is a separate candidate or diagnostic; the stock
baseline already includes MLX-LM native caching.
