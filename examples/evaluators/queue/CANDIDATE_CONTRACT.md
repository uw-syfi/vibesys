# Queue Candidate Contract v1

This document is the normative interface between the evaluator and an untrusted
queue implementation. Candidate behavior is accepted only when it satisfies
this contract; evaluator implementation details are described separately in
[`DESIGN.md`](DESIGN.md).

## Required artifact

Candidates must provide a shared library named `queue-candidate.so` in the
workspace root. The library may be implemented in any language or combination
of languages that exports the C ABI declared in
`include/vibesys_queue_abi.h`.

The queue uses VibeSys's `inprocess` execution mode: the evaluator loads and
invokes the library directly inside an evaluator-owned Rust process. The
candidate does not implement a network service. Process isolation used between
trusted evaluator components is not part of the candidate interface.

The header is authoritative for symbol names, signatures, constants, and ABI
version. This document is authoritative for lifecycle, concurrency, ownership,
semantic, and error requirements.

The evaluator's Rust runner is the only component that loads the library. During
correctness checks it runs in a worker process controlled by the Go Porcupine
checker. During performance measurement it calls the same ABI directly from
native producer and consumer threads.

## Lifecycle

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

## Operations and ownership

`vsq_try_enqueue` receives a byte pointer and length:

- `VSQ_OK` means the value was copied into the queue.
- `VSQ_FULL` means all item capacity was occupied or reserved and the call
  retained nothing.
- The input pointer is valid only for the duration of the call. The candidate
  must not retain or access it after returning.
- Length must not exceed the configured maximum value size.

`vsq_try_dequeue` receives caller-owned output storage:

- `VSQ_OK` means the complete oldest value was copied to `output` and
  `output_length` was set.
- `VSQ_EMPTY` means no published item was available. Output storage and
  `output_length` must remain unchanged.
- `VSQ_INVALID` means the output is null or too small for the oldest value. The
  value remains queued, and output storage and `output_length` remain unchanged.
- The candidate must not retain the output pointer after returning.
- A candidate must never write more than `output_capacity` bytes.

For valid enqueue inputs, only `VSQ_OK` and `VSQ_FULL` are normal results. For a
sufficient dequeue output, only `VSQ_OK` and `VSQ_EMPTY` are normal results.
`VSQ_INTERNAL_ERROR`, or `VSQ_INVALID` for otherwise valid arguments, fails
evaluation.

Operations are try-style: they do not wait for space to become free or for an
item to arrive. Successful operations preserve one global FIFO order and every
successfully enqueued item is returned exactly once.

SPSC and MPSC use an exact linearizable bounded-queue model. A successful
enqueue atomically appends an item, `VSQ_FULL` is legal only when the abstract
queue is at capacity, a successful dequeue atomically removes the oldest item,
and `VSQ_EMPTY` is legal only when the abstract queue is empty.

MPMC uses a reservation-aware bounded-queue model to match queues that acquire
capacity before copying and publishing an item. A successful enqueue has two
ordered internal events within its call interval:

1. reservation consumes one item of capacity;
2. publication makes the copied item visible at the FIFO tail.

`VSQ_FULL` is legal when published items plus unpublished reservations equal
capacity. `VSQ_EMPTY` is legal when no item has been published, even if an
enqueue currently holds a reservation. Consequently, a capacity-one MPMC
history may contain an in-flight enqueue that later returns `VSQ_OK`, an
overlapping enqueue that returns `VSQ_FULL`, and an intervening dequeue that
returns `VSQ_EMPTY`. Publication order defines the global FIFO order. Both
reservation and publication must occur within the successful enqueue call, so
a completed enqueue is visible to every later non-overlapping dequeue.

## Value-size guarantees

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
retention, undersized-output retry, concurrent short/full-buffer consumers, and
unchanged output on empty and invalid dequeues before running concurrent
Porcupine histories.

The evaluator may run multiple independent benchmark repetitions. Its
`total_ops_per_sec` field is the median repetition, and the JSON output includes
the individual `total_ops_per_sec_samples` values.

## Unsupported contracts

Version 1 does not support zero-copy ownership transfer, queue-managed producer
buffers, batched operations, or lossy queue semantics. A candidate must not
infer such behavior from implementation details. Any future contract with those
semantics requires a new version.

## Trust boundary

The Go correctness checker remains in a separate process from candidate native
code and owns expected payloads, call/return timestamps, histories, and the
Porcupine verdict. The measured Rust benchmark and candidate library share an
address space to avoid per-operation IPC and FFI transitions. Consequently,
performance scoring assumes cooperative native candidate code; Rust memory
safety does not isolate the runner from a hostile library.

For scored execution, the Go and Rust evaluators, this contract, and the ABI
header come from a trusted input snapshot.
