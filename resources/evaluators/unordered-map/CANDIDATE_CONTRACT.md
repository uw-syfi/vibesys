# Unordered Map Candidate Contract v1

This document is the normative interface between the evaluator and an untrusted
concurrent unordered map implementation.

## Required artifact

Candidates provide `unordered-map-candidate.so` in the workspace root and
export the C ABI declared by `include/vibesys_unordered_map_abi.h`. The header
is authoritative for symbols, signatures, statuses, and ABI version.

The evaluator loads the library directly into an evaluator-owned Rust process.
The candidate does not implement a service or communicate with the Go
correctness checker.

## Lifecycle

`vsum_abi_version` returns `VSUM_ABI_VERSION`. The runner calls
`vsum_map_create` with a maximum key size, maximum value size, and exact client
count. The map is unbounded in the number of keys.

The runner creates one client handle per native thread before use. Each handle
is confined to that thread. It destroys all clients before destroying the map.

## Operations

Keys and values are borrowed byte slices. Input pointers are valid only during
the call. The candidate must copy any bytes it retains and must never retain
output pointers or write past `output_capacity`.

`vsum_try_put` copies or replaces the mapping for `key`:

- `VSUM_OK` means the complete key and value were retained.
- `VSUM_INVALID` if a pointer is unusable, `key_len` exceeds the configured
  maximum key size, or `value_len` exceeds the configured maximum value size.
- Length zero is legal when the corresponding maximum is large enough.

`vsum_try_get` copies the current value for `key`:

- `VSUM_OK` copies the value and sets `output_length`.
- `VSUM_MISSING` leaves output storage and `output_length` unchanged.
- `VSUM_INVALID` for insufficient output leaves the mapping, output storage,
  and `output_length` unchanged.

`vsum_try_remove` copies the current value and deletes the mapping:

- `VSUM_OK` copies the old value, sets `output_length`, and removes the key.
- `VSUM_MISSING` leaves output storage and `output_length` unchanged.
- `VSUM_INVALID` for insufficient output leaves the mapping, output storage,
  and `output_length` unchanged.

There is no range, iteration, or ordering contract. Operations are try-style:
they do not wait for a mapping to appear or disappear.

Put, get, and remove must be linearizable. A successful put publishes the
complete mapping. A successful get returns the value of the most recent
linearized put of that key that has not been removed. `VSUM_MISSING` on get or
remove is legal only when that key has no published mapping. Operations on
different keys commute; the checker may partition by key as an implementation
of this spec, not as a weaker per-key relaxation.

## Value ownership

The correctness gate probes key and value lengths from zero through the
configured maxima, including non-word-aligned sizes. A caller may overwrite a
put input as soon as the call returns, and may overwrite get/remove output as
soon as those calls return. Copying and allocation are therefore part of
measured performance.

The benchmark currently uses fixed-size keys and values from 8 bytes through
1 MiB and reports the median `total_ops_per_sec` across requested repetitions.

## Trust boundary

The Go checker owns expected payloads, operation timestamps, histories, and
Porcupine verdicts in a separate process. The Rust benchmark and candidate
share an address space to avoid per-operation IPC, so scoring assumes
cooperative native candidate code.
