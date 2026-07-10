# VibeServe Queue Candidate ABI v1

Candidates provide a native shared library named `queue-candidate.so`. The
library may be implemented in any language that can export the C ABI declared
in `include/vibeserve_queue_abi.h`.

See [`DESIGN.md`](DESIGN.md) for the end-to-end architecture, trust model,
correctness protocol, benchmark design, and isolation limitations.

The trusted Rust runner is the only component that loads the library. During
correctness checks it runs in a worker process controlled by the Go Porcupine
checker. During performance measurement it calls the same ABI directly from
native producer and consumer threads.

## Queue lifecycle

`vsq_abi_version` must return `VSQ_ABI_VERSION`. The runner calls
`vsq_queue_create` with an item capacity, a maximum value size, and the exact
producer and consumer counts for the scenario. Queue capacity is measured in
items, not bytes.

The runner creates one opaque producer handle per producer and one opaque
consumer handle per consumer before timing begins. Each handle is used by
exactly one native thread, while different handles are called concurrently
against the same queue. An implementation that does not need per-thread state
may use thin handles that only reference the queue.

The runner destroys all producer and consumer handles before destroying the
queue. It drains the queue before normal destruction.

## Copying operations

`vsq_try_enqueue` receives a byte pointer and length:

- `VSQ_OK` means the value was copied into the queue.
- `VSQ_FULL` means the queue was full and retained nothing.
- The input pointer is valid only for the duration of the call. The candidate
  must not retain or access it after returning.
- Length must not exceed the configured maximum value size.

`vsq_try_dequeue` receives caller-owned output storage:

- `VSQ_OK` means the complete oldest value was copied to `output` and
  `output_length` was set.
- `VSQ_EMPTY` means the queue was empty. Output storage and `output_length`
  must remain unchanged.
- `VSQ_INVALID` means the output is null or too small for the oldest value. The
  value remains queued, and output storage and `output_length` remain unchanged.
- The candidate must not retain the output pointer after returning.
- A candidate must never write more than `output_capacity` bytes.

For valid enqueue inputs, only `VSQ_OK` and `VSQ_FULL` are normal results. For a
sufficient dequeue output, only `VSQ_OK` and `VSQ_EMPTY` are normal results.
`VSQ_INTERNAL_ERROR`, or `VSQ_INVALID` for otherwise valid arguments, fails
evaluation.

All operations are nonblocking. Successful operations must form a linearizable
bounded FIFO queue. Full and empty observations are part of that history.

## Value profiles

The evaluator selects a fixed `--value-size` for each run, currently bounded to
8 bytes through 1 MiB. Every measured value in a run has that size. Candidate
implementations must still honor any operation length up to the maximum passed
to `vsq_queue_create`.

The producer may overwrite its input immediately after enqueue returns, and
the consumer may overwrite its output immediately after dequeue returns. This
deliberately requires copying and makes allocation and byte movement part of
the measured implementation.

The correctness gate probes actual lengths 0, 1, 7, 8, 9, intermediate, and
maximum for 8-byte, 257-byte, and 1 MiB queue profiles. It also checks input
retention, undersized-output retry, and unchanged output on empty and invalid
dequeues before running concurrent Porcupine histories.

The trusted benchmark may run multiple independent repetitions. Its
`total_ops_per_sec` field is the median repetition, and the JSON output includes
the individual `total_ops_per_sec_samples` values.

## Trust boundary

The Go correctness checker remains in a separate process from candidate native
code and owns expected payloads, call/return timestamps, histories, and the
Porcupine verdict. The measured Rust benchmark and candidate library share an
address space to avoid per-operation IPC and FFI transitions. Consequently,
performance scoring assumes cooperative native candidate code; Rust memory
safety does not isolate the runner from a hostile library.

For scored execution, the Go and Rust evaluators and this ABI definition must
come from a trusted, immutable input copy.
