# Queue Default Harness

Resolves #43.

Reusable reference implementations, correctness checkers, and benchmark
drivers for the five initial VibeServe queue scenarios.

## Running the correctness checker

    python accuracy_checker/checker.py --scenario all
    python accuracy_checker/checker.py --scenario mpmc --producers 4 --consumers 4

Notes:
- `spsc`, `mpsc`, and `mpmc` scenarios collect concurrent operation histories and
  validate linearizability with [Porcupine](https://github.com/anishathalye/porcupine).
- `lossy` and `batch` scenarios use scenario-specific property checks.
- Go must be installed locally for the Porcupine-backed scenarios.

## Running the benchmark

    python benchmark/benchmark.py --scenario spsc --duration 10
    python benchmark/benchmark.py --scenario all --output-json results.json
    python benchmark/benchmark.py --scenario spsc --use-reference

## Acceptance criteria (from #43)

- Each reference implementation passes its scenario correctness checker.
- The benchmark runs each scenario without changing benchmark code.
- Scenario issues can reference this harness without restating shared terminology.
