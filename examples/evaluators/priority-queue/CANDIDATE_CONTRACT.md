# Priority Queue Candidate Contract v1

This document is the normative interface between the evaluator and an untrusted
bounded priority queue implementation.

## Required artifact

Candidates provide `priority-queue-candidate.so` in the workspace root and
export the C ABI declared by `include/vibesys_priority_queue_abi.h`. The header
is authoritative for symbols, signatures, statuses, and ABI version.

The evaluator loads the library directly into an evaluator-owned Rust process.
The candidate does not implement a service or communicate with the Go
correctness checker.

## Lifecycle

`vspq_abi_version` returns `VSPQ_ABI_VERSION`. The runner calls
`vspq_queue_create` with an item capacity, maximum value size, and exact
producer and consumer counts. Capacity is measured in items.

The runner creates one handle per producer and consumer before use. Each handle
is confined to one native thread. It destroys all handles before destroying the
queue and drains the queue before normal benchmark destruction.

## Operations

`vspq_try_enqueue` receives an unsigned 64-bit priority and a borrowed byte
slice:

- `VSPQ_OK` means the priority and a copy of the complete value were retained.
- `VSPQ_FULL` means the item capacity was occupied and nothing was retained.
- The input pointer is valid only during the call.
- Length must not exceed the configured maximum value size.

`vspq_try_dequeue` receives caller-owned output storage:

- `VSPQ_OK` copies and removes the queued value with the lowest numeric
  priority and sets `output_length`.
- Equal-priority values may be returned in any order.
- `VSPQ_EMPTY` leaves output storage and `output_length` unchanged.
- `VSPQ_INVALID` for insufficient output leaves the value, output storage, and
  `output_length` unchanged.
- The candidate never retains the output pointer or writes beyond
  `output_capacity`.

For valid enqueue inputs, only `VSPQ_OK` and `VSPQ_FULL` are normal. For a
sufficient dequeue output, only `VSPQ_OK` and `VSPQ_EMPTY` are normal.
Operations are try-style and must not wait for another operation to make
progress.

For SPSC, MPSC, and SPMC, the queue is linearizable and bounded. A successful
enqueue atomically inserts one `(priority, value)` item. `VSPQ_FULL` is legal
only at capacity. A successful dequeue atomically removes a minimum-priority
item, breaking ties arbitrarily. `VSPQ_EMPTY` is legal only when no item is
queued.

For MPMC, a successful enqueue may reserve capacity before publishing its item,
with both events occurring during the enqueue call. `VSPQ_FULL` observes
reserved plus published items, while dequeue and `VSPQ_EMPTY` observe only
published items. A successful dequeue removes any published item at the lowest
published numeric priority. Every successfully enqueued item is returned
exactly once in every scenario.

## Value ownership

The correctness gate probes lengths from zero through the configured maximum,
including non-word-aligned sizes. A producer may overwrite its input as soon as
enqueue returns, and a consumer may overwrite its output as soon as dequeue
returns. Copying and allocation are therefore part of measured performance.

The benchmark currently uses fixed-size values from 8 bytes through 1 MiB and
reports the median `total_ops_per_sec` across requested repetitions.

## Trust boundary

The Go checker owns expected payloads, operation timestamps, histories, and
Porcupine verdicts in a separate process. The Rust benchmark and candidate
share an address space to avoid per-operation IPC, so scoring assumes
cooperative native candidate code.
