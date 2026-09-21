# Unordered Map Evaluator Design

The evaluator separates correctness evidence from native throughput
measurement. Go owns history recording and Porcupine linearizability checking.
A trusted Rust runner owns dynamic loading and all candidate ABI calls.

This evaluator is distinct from `examples/data-structures/kvstore-default`,
which is a Python networked KV service. Here the candidate is an in-process
shared library exporting a copying C ABI.

## Correctness

Before concurrent histories, the runner probes ABI version, map/client
lifecycle, bad pointers, copied input ownership, output bounds, missing-key
behavior, and supported key and value lengths from zero through the configured
maxima. The checker then records call and return timestamps around operations.

The trusted gate checks linearizability of concurrent put/get/remove. Porcupine
partitions histories by key and steps a per-key register (missing, or present
with a byte-string value). Put publishes a value. Get and remove of a missing
key must report missing. Get of a present key must return that value. Remove of
a present key must return the old value and then leave the key missing.
Operations on different keys commute, so the partitioned search is the full
unordered-map linearizability spec, not a weaker per-key relaxation.

`swmr` histories use one writer client for put and remove, and the remaining
clients for get, against one shared map. `mw` histories use mixed put/get/remove
on every client.

Every history is decided with `CheckOperationsTimeout` under a budget, default
20 seconds per history and overridable with `--check-budget` on both `check`
and `benchmark`. The verdict is tri-state: `Ok` passes, `Illegal` fails as a
contract violation, and `Unknown` fails as an undecided history. Zero is
rejected because Porcupine treats a zero timeout as unlimited.

Candidate code runs only in the Rust worker. A crash, hang, malformed protocol
response, invalid ABI status, or failed model check rejects the run without
placing candidate code in the Go checker process.

`check` prints `PASS - {scenario} linearizable unordered map`.

## Benchmark

The native runner creates one client handle per thread, then calls the ABI
from those threads for the duration. On Linux each measured thread is pinned to
a CPU from the process affinity mask, cycling if there are more threads than
CPUs. On macOS, measured threads request user-interactive QoS. Payload copying,
failed lookups, and FFI transitions are included in elapsed time. Returned
values are checked against the encoded payload pattern. There is no item-count
conservation check, because the map is unbounded.

The `swmr` workload uses one writer thread doing put and remove, and the
remaining clients doing get. The `mw` workload uses N mixed clients, with
each thread choosing get 70%, put 20%, and remove 10%.

The benchmark command runs the linearizability gate first. Repeated
measurements report the median completed operation rate as `total_ops_per_sec`.

## Worker protocol

The checker talks to the native worker over Unix socketpairs, one per lane.
Concurrent histories use N lanes and N client handles (lane i uses client i).
Boundary histories use one mixed lane that can put, get, and remove.

Each request is a 16-byte little-endian header plus a key payload plus an
optional value payload:

| Offset | Type | Field |
| --- | --- | --- |
| 0 | u32 | operation: put=1, get=2, remove=3 |
| 4 | u32 | key_len |
| 8 | u32 | value_len |
| 12 | u32 | reserved, must be 0 |
| 16 | key_len bytes | key |
| 16+key_len | value_len bytes | value (put only) |

Each response is a 16-byte little-endian header plus an optional value payload:

| Offset | Type | Field |
| --- | --- | --- |
| 0 | u32 | status: ok=1, missing=2, invalid=3, error=4 |
| 4 | u32 | payload_len |
| 8 | u64 | reserved, must be 0 |
| 16 | payload_len bytes | value (ok get/remove) |

Get and remove requests must send `value_len = 0`. The worker always provides
an output buffer of the configured maximum value size, so a correctness
get/remove of a present key should not return `VSUM_INVALID`.

## Ownership

Files copied from `examples/evaluators/unordered-map` are trusted evaluator
inputs. The materialized `src/`, build files, and
`unordered-map-candidate.so` are candidate-owned.
