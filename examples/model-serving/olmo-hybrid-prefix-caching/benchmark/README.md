Benchmark for the Olmo-Hybrid-7B prefix-caching workload.

Run:
```
python benchmark.py --url http://localhost:8000
```

Workload:
- 32 768-token shared prefix (identical across all requests AND across
  invocations — `shared_seed=0`),
- 128-token unique tail per request,
- 128 output tokens, temperature 0, `ignore_eos`,
- 20 concurrent requests by default.

Headline metric: `aggregate_throughput_tok_per_sec` = `total_output_tokens / wall_clock`. Printed as `Primary metric: …` and included in the JSON output.

Server contract: OpenAI-compatible streaming `POST /v1/completions`. The benchmark sends `prompt` as a `list[int]` of token IDs (vLLM-style), so the server must accept that form (not just `str`).

Smoke run (judge sanity check):
```
python benchmark.py --url http://localhost:8000 --num-requests 2 --max-tokens 64
```
