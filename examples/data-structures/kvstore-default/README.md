# KV Store Default Harness

Reusable reference implementation, Porcupine-backed correctness checker, and
benchmark driver for a concurrent key-value store workload.

## Running the correctness checker

    uv run python accuracy_checker/checker.py --use-reference
    uv run python accuracy_checker/checker.py
    uv run python accuracy_checker/checker.py --clients 8 --ops 4000 --key-space 32

Notes:
- Use `--use-reference` to validate the bundled reference implementation.
- Omit `--use-reference` to check a candidate `main.py` exposing `VibeServeKVStore`.
- The checker records a concurrent operation history before validating linearizability.
- Linearizability is checked with [Porcupine](https://github.com/anishathalye/porcupine).
- Go must be installed locally to run the checker.

## Running the benchmark

    uv run python benchmark/benchmark.py --duration 10
    uv run python benchmark/benchmark.py --clients 8 --read-ratio 0.6 --output-json results.json
    uv run python benchmark/benchmark.py --use-reference
