# Stack Candidate Contract v1

This document is the normative interface between the evaluator and an untrusted
bounded stack implementation.

## Required artifact

Candidates provide `stack-candidate.so` in the workspace root and export the C
ABI declared by `include/vibesys_stack_abi.h`. The header is authoritative for
symbols, signatures, statuses, and ABI version.

The evaluator loads the library directly into an evaluator-owned Rust process.
The candidate does not implement a service or communicate with the Go
correctness checker.

## Lifecycle

`vsst_abi_version` returns `VSST_ABI_VERSION`. The runner calls
`vsst_stack_create` with an item capacity, maximum value size, and exact
producer and consumer counts. Capacity is measured in items.

The runner creates one handle per producer and consumer before use. Each handle
is confined to one native thread. It destroys all handles before destroying the
stack and drains the stack before normal benchmark destruction.

## Operations

`vsst_try_push` receives a borrowed byte slice:

- `VSST_OK` means a copy of the complete value was retained on the stack.
- `VSST_FULL` means the item capacity was occupied and nothing was retained.
- The input pointer is valid only during the call.
- Length must not exceed the configured maximum value size.

`vsst_try_pop` receives caller-owned output storage:

- `VSST_OK` copies and removes the most recently published value (LIFO) and
  sets `output_length`.
- Duplicate payloads occupy distinct stack slots. Pop order is LIFO by push
  linearization, not by value equality.
- `VSST_EMPTY` leaves output storage and `output_length` unchanged.
- `VSST_INVALID` for insufficient output leaves the value, output storage, and
  `output_length` unchanged.
- The candidate never retains the output pointer or writes beyond
  `output_capacity`.

For valid push inputs, only `VSST_OK` and `VSST_FULL` are normal. For a
sufficient pop output, only `VSST_OK` and `VSST_EMPTY` are normal. Operations
are try-style: they do not wait for space to become free or for an item to
arrive.

For SPSC, MPSC, and SPMC, the stack is linearizable and bounded. A successful
push atomically inserts one value at the top. `VSST_FULL` is legal only at
capacity. A successful pop atomically removes the top value. `VSST_EMPTY` is
legal only when no item is stacked.

For MPMC, a successful push may reserve capacity before publishing its item,
with both events occurring during the push call. `VSST_FULL` observes reserved
plus published items, while pop and `VSST_EMPTY` observe only published items.
A successful pop removes the last published item. Every successfully pushed
item is returned exactly once in every scenario.

## Value ownership

The correctness gate probes lengths from zero through the configured maximum,
including non-word-aligned sizes. A producer may overwrite its input as soon as
push returns, and a consumer may overwrite its output as soon as pop returns.
Copying and allocation are therefore part of measured performance.

The benchmark currently uses fixed-size values from 8 bytes through 1 MiB and
reports the median `total_ops_per_sec` across requested repetitions.

## Trust boundary

The Go checker owns expected payloads, operation timestamps, histories, and
Porcupine verdicts in a separate process. The Rust benchmark and candidate
share an address space to avoid per-operation IPC, so scoring assumes
cooperative native candidate code.
