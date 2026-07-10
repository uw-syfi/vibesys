# Queue Default Input

Resolves #43.

Default input for the SPSC, MPSC, and MPMC linearizable bounded-queue
scenarios. The trusted Go checker, Rust benchmark runner, and ABI definition
live in the `queue-input-core` package under `examples/libs/queue-input-core`;
this input depends on that package with a uv path dependency.

The shared seed at `examples/starters/queue-copying-rust` is copied into each
fresh workspace. Its editable `src/lib.rs` is deliberately simple Rust: one
`Mutex<VecDeque<Vec<u8>>>` shared by every producer and consumer. It is a
candidate baseline, not trusted code. From a materialized workspace, build the
required shared library with:

    make

## Running the correctness checker

    go -C _input_libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario all
    go -C _input_libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario mpmc --producers 4 --consumers 4

Notes:
- `make` creates the workspace's `./queue-candidate.so` shared library.
- `--use-reference` remains available only to self-test the trusted harness's
  internal model; it is not the editable candidate.
- The candidate ABI is documented at
  `_input_libs/queue-input-core/QUEUE_ABI.md` in a materialized workspace.
- The trusted harness records concurrent operation histories and validates
  linearizability with [Porcupine](https://github.com/anishathalye/porcupine).
- Boundary probes explicitly exercise empty, full, drain, and wraparound
  behavior before independently seeded concurrent histories.
- ABI probes cover copied values from zero bytes through 1 MiB, producer-buffer
  reuse, undersized dequeue retry, and unchanged empty outputs.
- Go and Rust must be installed locally for the trusted evaluator.

## Running the benchmark

    go -C _input_libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario spsc --duration 10s
    go -C _input_libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario all --output-json results.json
    go -C _input_libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario spsc

Use an odd `--repetitions` count to report the median run in
`total_ops_per_sec`; the manifest uses three repetitions and preserves every
sample in `total_ops_per_sec_samples`.

## Acceptance criteria (from #43)

- The harness's internal test implementation passes each scenario checker.
- The benchmark owns the measured interval, operation counts, and output JSON.
- Candidate stdout is diagnostic and cannot supply correctness or performance
  results.
