# Queue MPMC Input

This input targets a multi-producer, multi-consumer bounded FIFO queue. The
trusted Go harness is invoked by the manifest's Python wrappers. Candidates
implement the versioned shared-memory protocol documented at
`_input_libs/queue-input-core/QUEUE_PROTOCOL.md` and expose it through the fixed
`./queue-candidate` launcher.

Validate the bundled trusted reference:

    uv run python accuracy_checker/checker.py --use-reference
    uv run python benchmark/benchmark.py --use-reference --duration 1 --warmup 0
