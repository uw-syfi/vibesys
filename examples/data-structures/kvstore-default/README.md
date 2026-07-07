# KV Store Default Harness

Reusable reference implementation, Porcupine-backed correctness checker, and
benchmark driver for a concurrent key-value store workload.

## Running the correctness checker

    python accuracy_checker/checker.py
    python accuracy_checker/checker.py --clients 8 --ops 4000 --key-space 32

Notes:
- The checker records a concurrent operation history from `main.VibeServeKVStore`.
- Linearizability is checked with [Porcupine](https://github.com/anishathalye/porcupine).
- Go must be installed locally to run the checker.

## Running the benchmark

    python benchmark/benchmark.py --duration 10
    python benchmark/benchmark.py --clients 8 --read-ratio 0.6 --output-json results.json
    python benchmark/benchmark.py --use-reference
