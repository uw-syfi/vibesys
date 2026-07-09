# Queue Default Input

Resolves #43.

Default input for the SPSC, MPSC, and MPMC linearizable bounded-queue
scenarios. The trusted Go checker, benchmark, reference server, and protocol
definition live in the `queue-input-core` package under
`examples/libs/queue-input-core`; this input depends on that package with a uv
path dependency.

## Running the correctness checker

    uv run python accuracy_checker/checker.py --use-reference --scenario all
    uv run python accuracy_checker/checker.py --scenario all
    uv run python accuracy_checker/checker.py --scenario mpmc --producers 4 --consumers 4

Notes:
- Use `--use-reference` to validate the bundled reference server.
- Omit `--use-reference` to check the workspace's executable
  `./queue-candidate` launcher.
- The candidate protocol is documented at
  `_input_libs/queue-input-core/QUEUE_PROTOCOL.md` in a materialized workspace.
- The trusted harness records concurrent operation histories and validates
  linearizability with [Porcupine](https://github.com/anishathalye/porcupine).
- Boundary probes explicitly exercise empty, full, drain, and wraparound
  behavior before independently seeded concurrent histories.
- Go must be installed locally for the Porcupine-backed scenarios.

## Running the benchmark

    uv run python benchmark/benchmark.py --scenario spsc --duration 10
    uv run python benchmark/benchmark.py --scenario all --output-json results.json
    uv run python benchmark/benchmark.py --scenario spsc --use-reference

## Acceptance criteria (from #43)

- The trusted reference passes each scenario correctness checker.
- The benchmark owns the measured interval, operation counts, and output JSON.
- Candidate stdout is diagnostic and cannot supply correctness or performance
  results.
