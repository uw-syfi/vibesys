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

By default the framework gate runs 16 real TraceLab sessions with up to 8 active
sessions at once. This keeps scoring trace-shaped while giving vLLM enough
independent chats to exercise continuous batching during tool waits and long
prefills. Lower `--max-sessions` / `--max-active-sessions` manually for quick
smoke tests.
