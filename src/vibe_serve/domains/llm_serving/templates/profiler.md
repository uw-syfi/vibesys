## LLM-serving profile capture

Use the benchmark's steady-state serving path when collecting profile evidence. If the profiler strategy supports only one process, run the server under the profiler and drive load with the benchmark in a second shell. Discover flags with `--help`; do not assume every benchmark accepts the same request-count or token flags.

For local server-style captures, the usual shape is:

1. Read `main.py` to understand startup and port.
2. Kill prior servers: `pkill -f "python main.py" 2>/dev/null || true; sleep 2`.
3. Pre-warm — first-time kernel compilation or model load can take minutes.
4. Start the candidate server under the profiler.
5. Drive load using the benchmark, for example `uv run python {{ bench_path or 'bench' }}/benchmark.py --url http://localhost:8077 --rate 1 --num-requests 5 --max-tokens 64` when those flags exist.
6. Stop the profiled server and analyze the report.

For torch in-process captures, the reference harness is designed around `VibeServeModel.from_pretrained(...)` and `.generate(...)`:

```
python torch_profiler/analyze_torch_profile.py capture \
  --model-dir /workspace --weights-dir /model \
  --output /tmp/prof.json \
  --warmup 3 --num-iters 20 --max-tokens 32 \
  --prompt "The capital of France is"
```

Use this mode for kernel-level optimization (fused norm/rope/attention, CUDA graphs, dtypes). It does not cover HTTP, batching, or queueing overhead.

For Modal torch profiling, the implementer's `main.py` is required to expose `@app.local_entrypoint() modal_profile(output, num_iters, max_tokens, prompt)`. Invoke it from the editor container:

```
modal run main.py::modal_profile -- \
  --output /workspace/prof.json \
  --num-iters 20 \
  --max-tokens 32 \
  --prompt "The capital of France is"
```

This dispatches to a `@app.function profile_remote(...)` running on the Modal GPU, which wraps the same workload the benchmark exercises in `torch.profiler` and returns the analyzer-compatible JSON.
