# TraceLab replay benchmark

`benchmark.py` is a thin shim over the hidden TraceLab replay evaluator. The
framework injects the hidden evaluator path only when it runs the trusted
benchmark gate. The hidden evaluator downloads the pinned TraceLab public
DuckDB release, verifies its SHA256, converts it with TraceLab's CSV exporter,
and directly invokes TraceLab's Rust `session_runner`.

Run:

```bash
uv run python benchmark/benchmark.py \
  --url http://localhost:8000 \
  --model Qwen/Qwen3-Coder-30B-A3B-Instruct-FP8 \
  --output-json /tmp/tracelab_replay.json
```

The primary metric is `aggregate_output_tokens_per_second`.

By default the framework gate runs 8 real TraceLab sessions with up to 8 active
sessions at once. This keeps scoring trace-shaped while giving vLLM enough
independent chats to exercise continuous batching during tool waits and long
prefills. Lower `--max-sessions` / `--max-active-sessions` manually for quick
smoke tests; increase `--max-sessions` for longer saturation studies.

Controlled Modal H100 replay results on this 8x8 gate:

- vLLM single-container baseline: 280.81 output tok/s, p90 TTFT 17.24s, server
  prefix hit rate 75.59%.
- TraceLab-specialized starter with admission-time token-prompt cache
  accounting and `gpu_memory_utilization=0.95`: 302.51 output tok/s, p90 TTFT
  1.97s, server prefix hit rate 90.61%.
