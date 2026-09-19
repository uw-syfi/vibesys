# KV Store Default Harness

Reusable reference implementation, Porcupine-backed correctness checker, and
benchmark driver for a concurrent key-value store workload.

## Layout

Evaluator files live in `.vibesys/tasks/default/` (single task, so
`--input examples/data-structures/kvstore-default` selects it).

## Running the correctness checker

    uv run python .vibesys/tasks/default/accuracy_checker/checker.py --use-reference
    uv run python .vibesys/tasks/default/accuracy_checker/checker.py
    uv run python .vibesys/tasks/default/accuracy_checker/checker.py --clients 8 --ops 4000 --key-space 32

Notes:
- Use `--use-reference` to validate the bundled reference implementation.
- Omit `--use-reference` to check a candidate `main.py` exposing `VibeSysKVStore`.
- The checker records a concurrent operation history before validating linearizability.
- Linearizability is checked with [Porcupine](https://github.com/anishathalye/porcupine).
- Go must be installed locally to run the checker.

## Running the benchmark

    uv run python .vibesys/tasks/default/benchmark/benchmark.py --duration 10
    uv run python .vibesys/tasks/default/benchmark/benchmark.py --clients 8 --read-ratio 0.6 --output-json results.json
    uv run python .vibesys/tasks/default/benchmark/benchmark.py --use-reference
