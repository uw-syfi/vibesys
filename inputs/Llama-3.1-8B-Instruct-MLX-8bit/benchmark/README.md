Benchmark inputs for the MLX 8-bit Llama JSON-schema target.

Run:

```bash
python benchmark.py \
  --url http://localhost:8000 \
  --closed-loop \
  --max-tokens 256 \
  --output-json /tmp/llama_mlx_8bit_bench.json
```

The benchmark posts streaming completion requests to `/v1/completions` with
schemas from `epfl-dlab/JSONSchemaBench`, pinned to revision
`5bd0f4640badc6f3f02df796421d21cb0ca0b141`, in `response_format`. It reports
schema-valid rate, output-token throughput, TTFT, TPOT, and end-to-end latency.
The default subset is `full` and default split is `val`; use
`--dataset-subset`, `--split`, `--limit`, and `--dataset-cache-dir` to control
the dataset slice and cache location.
