# Queue Default Input

Resolves #43.

Default input for the SPSC, MPSC, and MPMC linearizable bounded-queue
scenarios. The trusted Go checker, Rust benchmark runner, and ABI definition
live under `examples/evaluators/queue`. VibeServe materializes that source at
`_evaluator/queue` separately from the editable candidate workspace seed.

The shared seed at `examples/starters/queue-rs` is copied into each
fresh workspace. Its editable `src/lib.rs` is deliberately simple Rust: one
`Mutex<VecDeque<Vec<u8>>>` shared by every producer and consumer. It is a
candidate baseline, not trusted code. From a materialized workspace, build the
required shared library with:

    make

## Running the correctness checker

    go -C _evaluator/queue run . check --workspace "$PWD" --scenario all
    go -C _evaluator/queue run . check --workspace "$PWD" --scenario mpmc --producers 4 --consumers 4

Notes:
- `make` creates the workspace's `./queue-candidate.so` shared library.
- `--use-reference` remains available only to self-test the evaluator's
  internal model; it is not the editable candidate.
- The candidate ABI is documented at
  `_evaluator/queue/CANDIDATE_CONTRACT.md` in a materialized workspace.
- The evaluator architecture and trust model are documented at
  `_evaluator/queue/DESIGN.md`.
- The trusted evaluator records concurrent operation histories and validates
  linearizability with [Porcupine](https://github.com/anishathalye/porcupine).
- Boundary probes explicitly exercise empty, full, drain, and wraparound
  behavior before independently seeded concurrent histories.
- ABI probes cover copied values from zero bytes through 1 MiB, producer-buffer
  reuse, undersized dequeue retry, and unchanged empty outputs.
- Go and Rust must be installed locally for the trusted evaluator.

## Running the benchmark

    go -C _evaluator/queue run . benchmark --workspace "$PWD" --scenario spsc --duration 10s
    go -C _evaluator/queue run . benchmark --workspace "$PWD" --scenario all --output-json results.json
    go -C _evaluator/queue run . benchmark --workspace "$PWD" --scenario spsc

Use an odd `--repetitions` count to report the median run in
`total_ops_per_sec`; the manifest uses three repetitions and preserves every
sample in `total_ops_per_sec_samples`.

## Acceptance criteria (from #43)

- The evaluator's internal test implementation passes each scenario checker.
- The benchmark owns the measured interval, operation counts, and output JSON.
- Candidate stdout is diagnostic and cannot supply correctness or performance
  results.
