# Queue Default Input

Resolves #43.

Default input for the SPSC, MPSC, and MPMC bounded FIFO queue scenarios. The reusable
reference implementations, correctness checker, and benchmark driver live in
the `queue-input-core` package under `examples/libs/queue-input-core`; this input
depends on that package with a uv path dependency.

## Running the correctness checker

    uv run python accuracy_checker/checker.py --use-reference --scenario all
    uv run python accuracy_checker/checker.py --scenario all
    uv run python accuracy_checker/checker.py --scenario mpmc --producers 4 --consumers 4

Notes:
- Use `--use-reference` to validate the bundled reference implementations.
- Omit `--use-reference` to check a candidate `main.py` exposing `VibeServeQueue`.
- Every scenario collects concurrent operation histories and validates
  linearizability with [Porcupine](https://github.com/anishathalye/porcupine).
- Go must be installed locally for the Porcupine-backed scenarios.

## Running the benchmark

    uv run python benchmark/benchmark.py --scenario spsc --duration 10
    uv run python benchmark/benchmark.py --scenario all --output-json results.json
    uv run python benchmark/benchmark.py --scenario spsc --use-reference

## Acceptance criteria (from #43)

- Each reference implementation passes its linearizability checker.
- The benchmark runs each scenario without changing benchmark code.
- Scenario issues can reference this input core without restating shared terminology.
