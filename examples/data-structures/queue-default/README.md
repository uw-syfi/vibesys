# Queue Default Input

Resolves #43.

Default input for the SPSC, MPSC, and MPMC linearizable bounded-queue
scenarios. The trusted Go checker, benchmark, reference server, and protocol
definition live in the `queue-input-core` package under
`examples/libs/queue-input-core`; this input depends on that package with a uv
path dependency.

## Running the correctness checker

    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --use-reference --scenario all
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario all
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario mpmc --producers 4 --consumers 4

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

    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario spsc --duration 10s
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario all --output-json results.json
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario spsc --use-reference

## Acceptance criteria (from #43)

- The trusted reference passes each scenario correctness checker.
- The benchmark owns the measured interval, operation counts, and output JSON.
- Candidate stdout is diagnostic and cannot supply correctness or performance
  results.
