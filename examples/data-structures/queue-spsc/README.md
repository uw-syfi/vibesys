# Queue SPSC Input

This input targets a single-producer, single-consumer bounded FIFO queue. The
manifest invokes the trusted Go harness directly. Candidates implement the
copying C ABI documented at `_input_libs/queue-input-core/QUEUE_ABI.md` and
export it from `./queue-candidate.so`.

The editable `src/lib.rs` is an intentionally naive Rust candidate using one
mutex and `VecDeque`. Build and validate it from this directory:

    make
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario spsc
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario spsc --duration 1s --warmup 0s

The starter is untrusted and may be replaced with any implementation that
exports the same ABI. `--use-reference` only self-tests the harness's internal
model; it is not the optimization starting point. The manifest benchmark uses
three repetitions and reports their median as `total_ops_per_sec`.
