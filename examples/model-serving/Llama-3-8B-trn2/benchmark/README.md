# Benchmark

Request Factory owns synthetic token generation, `/v1/completions` transport,
and per-request timing. The bundle wrapper executes the full matrix of input /
output lengths `128,256,512` by concurrency `1,2,4,8`. Every matrix point uses
64 independent requests, RF saturated arrival mode, and the declared maximum
concurrency. It retains every matrix result and selects the highest
output-token throughput as `aggregate_throughput`.
The wrapper partitions one logical trace across all 12 points. RF therefore
assigns every request a matrix-wide ordinal and a cache-distinct deterministic
prompt instead of replaying concurrency 1's prompts at concurrency 2, 4, and
8. Every point uses the same power-of-two token pool, sized for the longest
prompt and the complete matrix. An RF warning that the pool is too short fails
the point.

This replaces the prior duration-driven sweep and its bundle-local tokenizer
fallback with a fixed-volume RF workload. The original request lengths,
concurrency matrix, and zero-temperature setting are preserved. Comparisons
with historical scores should account for the fixed-volume methodology. The
evaluator uses the tokenizer from the pinned model mounted at `/model`; manual
runs outside that container must pass `--tokenizer` explicitly.

The VibeSys benchmark result is rejected if RF reports any failed or
output-mismatched request, if the expected trace count is incomplete, or if the
request log does not show the declared input and output lengths. For CPU-only
contract testing, run the shared fake-server smoke with a local Request Factory
engine:

```bash
uv run python -m tests.examples.request_factory_cpu_smoke \
  --profile examples/model-serving/Llama-3-8B-trn2/benchmark/cpu_smoke.toml \
  --request-factory-engine <RF_ENGINE>
```
