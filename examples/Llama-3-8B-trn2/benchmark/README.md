Benchmark for the Trainium Llama-3-8B server.

Fixed-length Poisson sweep: fixed input/output token lengths (default
128/256/512, input_len == output_len) crossed with Poisson request rates up
to 2.0 req/s, driving the OpenAI-compatible HTTP server over `/v1/completions`.

Run:
    python benchmark.py --url http://localhost:8000 \
        --lengths 128,256,512 --rates 0.5,1.0,2.0 \
        --requests-per-scenario 16 --output-json /tmp/bench.json

Headline metric: `aggregate_throughput` (output tok/s) in the `--output-json`.
