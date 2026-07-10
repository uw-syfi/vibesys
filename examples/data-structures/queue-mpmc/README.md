# Queue MPMC Input

This input targets a multi-producer, multi-consumer bounded FIFO queue. The
manifest invokes the trusted Go harness directly. Candidates implement the
copying C ABI documented at `_input_libs/queue-input-core/QUEUE_ABI.md` and
export it from `./queue-candidate.so`.

Validate the bundled trusted reference:

    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario mpmc --use-reference
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario mpmc --use-reference --duration 1s --warmup 0s
