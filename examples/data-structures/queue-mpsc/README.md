# Queue MPSC Input

This input targets a multi-producer, single-consumer bounded FIFO queue. The
manifest invokes the trusted Go harness directly. Candidates implement the
versioned shared-memory protocol documented at
`_input_libs/queue-input-core/QUEUE_PROTOCOL.md` and expose it through the fixed
`./queue-candidate` launcher.

Validate the bundled trusted reference:

    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . check --workspace "$PWD" --scenario mpsc --use-reference
    go -C ../../libs/queue-input-core/src/queue_input_core/trusted_harness run . benchmark --workspace "$PWD" --scenario mpsc --use-reference --duration 1s --warmup 0s
