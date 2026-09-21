# Ordered Map Candidate Contract v1

This document is the normative interface between the evaluator and an untrusted
ordered map implementation. Ordered neighbor and range operations are required;
a point-only map is not a valid candidate.

## Required artifact

Candidates provide `ordered-map-candidate.so` in the workspace root and export
the C ABI declared by `include/vibesys_ordered_map_abi.h`. The header is
authoritative for symbols, signatures, statuses, and ABI version.

The evaluator loads the library directly into an evaluator-owned Rust process.
The candidate does not implement a service or communicate with the Go
correctness checker.

## Lifecycle

`vsom_abi_version` returns `VSOM_ABI_VERSION` (1). The runner calls
`vsom_map_create` with a maximum key size, maximum value size, and exact client
count. The map is unbounded: there is no item-capacity argument.

The runner creates one client handle per configured client before use. Each
handle is confined to one native thread. It destroys all handles before
destroying the map.

## Statuses

- `VSOM_OK` (0): the operation completed and wrote the documented outputs.
- `VSOM_MISSING` (1): no matching mapping. Caller output storage is unchanged.
- `VSOM_INVALID` (2): bad arguments or undersized output. Mapping and caller
  output storage are unchanged.
- `VSOM_INTERNAL_ERROR` (3): candidate failure. The run is rejected.

Operations are try-style: they do not wait for a mapping to appear or
disappear.

## Keys and values

Keys and values are copied byte strings. Keys compare as unsigned lexicographic
byte order: the first differing byte is compared as `uint8_t`, and a proper
prefix is less than the longer key.

Input pointers are valid only during the call. Lengths must not exceed the
configured maxima. A caller may overwrite its inputs as soon as a call returns,
and may overwrite its outputs as soon as a successful call returns. Copying is
part of measured performance.

Empty keys and empty values are legal when their length is zero, including a
null data pointer with length zero.

## Point operations

`vsom_try_put` copies the key and value and inserts or replaces that mapping.
Normal status is `VSOM_OK`. Oversize inputs are `VSOM_INVALID`.

`vsom_try_get` and `vsom_try_remove` copy the current value into caller storage:

- `VSOM_OK` writes `value_len` and, for remove, deletes the mapping.
- `VSOM_MISSING` leaves output storage and `value_len` unchanged.
- `VSOM_INVALID` for an undersized output or an oversize query key leaves the
  mapping, output storage, and `value_len` unchanged. The candidate never
  retains the output pointer or writes beyond `value_cap`.

## Ordered operations

All ordered operations copy into caller storage.

`vsom_try_min` and `vsom_try_max` return the least or greatest key and its
value. An empty map returns `VSOM_MISSING` and leaves outputs unchanged.

`vsom_try_predecessor` returns the greatest key strictly below the query.
`vsom_try_successor` returns the least key strictly above the query. A missing
neighbor returns `VSOM_MISSING` and leaves outputs unchanged. An oversize query
key is `VSOM_INVALID` and leaves outputs unchanged.

`vsom_try_range` fills caller buffers with mappings whose keys lie in the
half-open interval `[start, end)`, in increasing key order, plus the truncation
remaining flag. There is no callback and no cursor object. Sequential range
(no overlapping put or remove) is a snapshot. Concurrent range is weakly
consistent skip-list iteration: items are sorted and unique, each key is in
`[start, end)`, and the walk may miss or observe keys that concurrent
mutations insert or delete. It is not a linearizable snapshot.

- `keys_out` is `key_stride * max_items` bytes. Item `i` occupies
  `keys_out + i * key_stride`.
- `vals_out` is `val_stride * max_items` bytes.
- `lengths_out_key` and `lengths_out_val` are `uint64_t[max_items]`.
- On `VSOM_OK`, `count_out` is the number of items written. Sequential range
  sets `remaining_out` to 1 iff more in-range keys exist than `max_items`,
  else 0. Concurrent range may set `remaining_out` to 0 even when more keys
  existed; `remaining_out` 1 is allowed only when `count_out` equals
  `max_items` (or `max_items` is 0).
- A sequential empty range is `VSOM_OK` with `count_out` 0 and `remaining_out`
  0. A concurrent empty walk may also report that.
- An oversize `start` or `end` is `VSOM_INVALID` and no outputs are written.
- If any item that would be written does not fit in its stride, status is
  `VSOM_INVALID` and no outputs are written.

`max_items` 0 is `VSOM_OK` with `count_out` 0. Sequential range sets
`remaining_out` to 1 when the interval is nonempty.

## Correctness

Point operations (put, get, remove) are linearizable against a sequential
sorted map of unique keys. Keys compare as unsigned lexicographic byte order
(`bytes.Compare`): the first differing byte is compared as `uint8_t`, and a
proper prefix is less than the longer key.

Ordered operations (min, max, predecessor, successor, range) that do not
overlap a put or remove match that sequential spec, including range as a
snapshot of `[start, end)` truncated to `max_items` plus `remaining_out`.
Ordered operations that overlap a mutation are weakly consistent: they need
not be a linearizable snapshot. Range items must still be unique, strictly
increasing, and inside `[start, end)`. Predecessor keys are strictly below the
query; successor keys are strictly above it. This is the iteration contract of
concurrent skip lists such as oneTBB `concurrent_map`.

## Trust boundary

The Go checker owns expected payloads, operation timestamps, histories, and
Porcupine verdicts in a separate process. The Rust benchmark and candidate
share an address space to avoid per-operation IPC, so scoring assumes
cooperative native candidate code.
