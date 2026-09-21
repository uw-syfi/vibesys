# Ordered Map Evaluator Design

The evaluator separates concurrent linearizability evidence from native
throughput measurement. Go owns workload generation, histories, and Porcupine
verdicts. A trusted Rust runner owns dynamic loading and all candidate ABI
calls.

## Correctness

The gate is ABI probing plus linearizability of point operations against one
sequential sorted-map specification. Ordered operations that do not overlap a
put or remove are checked against that same spec. Ordered operations that
overlap a mutation are weakly consistent skip-list iteration, not snapshots.
Histories are checked with Porcupine's `CheckOperationsTimeout` on the full
object. There is no per-key `Partition`: min, max, predecessor, successor, and
range observe other keys, so splitting the history by key would hide the
ordered behavior the sequential ABI probe and boundary history still require.

The sequential spec is an in-memory list of unique keys kept in unsigned
lexicographic order (`bytes.Compare`). Put inserts or replaces. Get and remove
must match the spec's `MISSING` versus `OK` status and, on `OK`, the spec
payload. Non-overlapping min, max, predecessor, successor, and range must
match the spec the same way. Range is then a snapshot: at that linearization
point the returned items must equal `rangeQuery(start, end, max_items)`,
including the truncation remaining flag, and items must be strictly
increasing. An empty sequential range is `OK` with zero items and remaining
false.

A range, min, max, predecessor, or successor whose call interval overlaps a
put or remove is marked weak. Weak range still requires unique strictly
increasing keys inside `[start, end)` and `remaining_out` 1 only when
`count_out` equals `max_items` (or `max_items` is 0). It may miss keys or
observe keys a concurrent mutation inserted or deleted, and `remaining_out` 0
does not assert completeness. Weak neighbor ops may return `OK` or `MISSING`
without matching the spec's current min/max/neighbor; a predecessor key must
still be strictly below the query and a successor key strictly above it. This
matches oneTBB `concurrent_map` iterators.

Porcupine's search is worst-case exponential in overlapping operations.
Histories are therefore small (at most 32 operations, range and neighbor ops
kept to a minority, `max_items` in 1-4). Every history is decided under a time
budget, default 20 seconds, overridable with `--check-budget` on both `check`
and `benchmark`. `Unknown` (budget expired) fails the gate; it is not a pass.
Zero is rejected because Porcupine treats a zero timeout as unlimited.

`check` prints `PASS - {scenario} linearizable point map with weakly consistent ordered operations`.

The runner probes lifecycle, copied input ownership, output bounds, empty
min/max, missing predecessor/successor, range over empty, singleton, and
multi-key maps, the range truncation remaining flag, oversize query keys, and
supported key and value lengths.

The checker then records call and return timestamps around Go-driven
operations. A mixed single-lane, single-client session replays a fixed boundary
history covering every op kind. Concurrent trials use N lanes and N clients:
SWMR keeps client 0 on put/remove while the others issue get plus ordered ops,
and MW mixes every op on every client.

Candidate code runs only in the Rust worker. A crash, hang, malformed protocol
response, invalid ABI status, or failed model check rejects the run without
placing candidate code in the Go checker process.

## Protocol

Go talks to the worker over one Unix socketpair per lane using a 16-byte
little-endian header plus a key frame plus a value frame:

- request: `operation:u32`, `key_len:u32`, `value_len:u32`, `extra:u32`, key,
  value
- response: `status:u32`, `key_len:u32`, `value_len:u32`, `extra:u32`, key,
  value

`extra` is `max_items` on range requests and the remaining flag on range
responses. Range payloads pack `count:u64` followed by `key_len:u64`,
`value_len:u64`, key, value for each item. Lane `i` uses client handle `i` and
file descriptor `fd_base + i`. Mixed single-lane mode serves boundary histories
from one client that may issue every operation.

## Benchmark

The native runner creates client handles once, then calls the ABI from the
thread counts selected by the scenario. On Linux each measured thread is pinned
to a CPU from the process affinity mask, cycling if there are more threads than
CPUs. On macOS, measured threads request user-interactive QoS. Payload copying,
failed lookups, ordered operations, and FFI transitions are included in elapsed
time.

The benchmark command runs the linearizability gate first. Repeated
measurements report the median completed-operation rate as `total_ops_per_sec`.

`--reference` uses an internal `Mutex<BTreeMap<Vec<u8>, Vec<u8>>>`. It is not
the optimization starting point.

### Mix ratios

Key universe: 256 keys of the configured key size. Values use the configured
value size.

`swmr` (one writer, remaining threads readers):

- writer: 70% put, 30% remove
- readers: 70% get, 15% successor, 15% range

`mw` (every thread mixed):

- 30% get, 25% put, 15% remove, 15% successor, 15% range

JSON is analogous to the priority-queue evaluator, with point and ordered-op
counts instead of enqueue/dequeue counts.

## Ownership

Go owns histories, the sequential spec, and Porcupine verdicts. Rust owns FFI,
native handle lifetimes, ABI probes, and the timed benchmark. Files copied from
`examples/evaluators/ordered-map` are trusted evaluator inputs. The materialized
`src/`, build files, and `ordered-map-candidate.so` are candidate-owned.
