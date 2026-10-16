# Queue MPMC Task

This task targets a multi-producer, multi-consumer reservation-aware bounded
FIFO queue. Successful enqueues reserve capacity before publication; `FULL`
counts reservations while `EMPTY` observes only published items. Candidates
implement the evaluator's copying C ABI and export it from
`./queue-candidate.so`.

The repository provides an editable `src/lib.rs` with an intentionally naive
Rust candidate using one mutex and `VecDeque`. Build it from the repository
root:

    make

The task declares this build (`[build]` in `vibesys.input.toml`): VibeSys runs
it confined in a fresh checkout of each measured commit, verifies that it
produced `queue-candidate.so`, and only then runs the check and the benchmark.

VibeSys resolves the evaluator package during validation,
then runs it during optimization. The evaluator command is not a repository
executable.

The repository implementation is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the evaluator's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
