# Benchmark

Request Factory owns synthetic token generation, `/v1/completions` transport,
and per-request timing. The bundle wrapper executes the full matrix of input /
output lengths `128,256,512` by concurrency `1,2,4,8`. Every matrix point uses
64 independent requests, RF saturated arrival mode, and the declared maximum
concurrency. It retains every matrix result and selects the highest
output-token throughput as `aggregate_throughput`.
Each matrix point writes a deterministic temporary corpus and sets RF's token
pool limit to the larger of twice that point's input length or the request
count. An RF warning that the pool is too short fails the point.

This replaces the prior duration-driven sweep and its bundle-local tokenizer
fallback with a fixed-volume RF workload. The original request lengths,
concurrency matrix, and zero-temperature setting are preserved. Comparisons
with historical scores should account for the fixed-volume methodology.

The VibeSys benchmark result is rejected if RF reports any failed or
output-mismatched request, if the expected trace count is incomplete, or if the
request log does not show the declared input and output lengths. For a CPU-only
contract check, use a local tokenizer and deterministic OpenAI-compatible
completions fake with the benchmark adapter.
