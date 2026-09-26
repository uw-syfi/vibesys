# Qwen3.6-35B-A3B Request Factory Benchmark

Request Factory replays 64 independent streamed `/v1/completions` requests
under saturated load, with maximum concurrency 16. Each prompt has 256 tokens
and requests up to 128 output tokens. The workload targets the from-scratch
sparse-MoE Qwen3.6-35B-A3B server across 2 H100s.

Exact token lengths replace the old approximate word count, and the fixed
trace replaces the legacy 30-second duration window. RF counts completion
token IDs rather than nonempty SSE chunks and reports p90 rather than p99
latency. These methodology changes are intentional; old and new scores are
not directly comparable. The bundle adapter validates the RF summary and maps
the metrics to VibeSys result protocol 2.

The adapter generates its deterministic synthetic text corpus in a temporary
directory and sets an explicit token-pool limit from prompt length and request
count. It fails if RF reports that the pool is too short for a prompt.

The evaluator injects the trusted RF engine path. For CPU-only request-path
validation, run `python benchmark/cpu_smoke.py --request-factory-engine
<RF_ENGINE>`; it uses a strict local fake completions server and a tiny local
tokenizer fixture. Fake server metrics are not serving-performance results.
