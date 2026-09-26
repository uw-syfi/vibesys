# Qwen3.5-397B-A17B Request Factory Benchmark

Request Factory replays 64 independent streamed `/v1/completions` requests
under saturated load, with maximum concurrency 48. Each prompt has 2048 tokens
and requests up to 512 output tokens. The workload targets the from-scratch
hybrid Gated-DeltaNet/MoE Qwen3.5-397B-A17B server across 4 B200s.

Exact token lengths replace the old approximate word count, and the fixed
trace replaces the legacy 90-second duration window. RF counts completion
token IDs rather than nonempty SSE chunks and reports p90 rather than p99
latency. These methodology changes are intentional; old and new scores are
not directly comparable. The bundle adapter validates the RF summary and maps
the metrics to VibeSys result protocol 2.

The evaluator injects the trusted RF engine path. For CPU-only request-path
validation, run `python benchmark/cpu_smoke.py --request-factory-engine
<RF_ENGINE>`; it uses a strict local fake completions server and a tiny local
tokenizer fixture. The benchmark generates its deterministic text corpus in
the per-run temporary directory and sets the token-pool limit to the larger of
twice the prompt length or the request count. It fails if RF warns that the
pool is shorter than a prompt. Fake server metrics are not serving-performance
results.
