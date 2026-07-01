Benchmark for the Trainium Llama-3-8B server.

Warm, closed-loop throughput. Fixed input/output token lengths (default
128/256/512). It **warms up** first (compiles each bucket; untimed) and then
measures **closed-loop concurrency** (keeps C requests in flight for a fixed
duration, sweeping C) so the headline reflects *steady-state* throughput, not
cold-compile or queue-wait time.

Run:
    python benchmark.py --url http://localhost:8000 \
        --lengths 128,256,512 --concurrency 1,2,4,8 \
        --duration 20 --warmup-requests 2 --output-json /tmp/bench.json

Headline metric: `aggregate_throughput` = peak steady-state output tok/s across
the (length, concurrency) sweep.
