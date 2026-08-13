# Priority Queue Evaluator Design

The evaluator separates correctness evidence from native throughput
measurement. Go owns workload generation and linearizability checking. A
trusted Rust runner owns dynamic loading and all candidate ABI calls.

## Correctness

Before concurrent histories, the runner probes lifecycle, copied input
ownership, output bounds, empty behavior, full behavior, and supported value
lengths. The checker then records call and return timestamps around concurrent
operations. SPSC, MPSC, and SPMC histories are validated with Porcupine against
an exact bounded priority queue model. MPMC histories use a reservation-aware
model that splits successful enqueues into capacity reservation and publication
events within the original call interval.

Both models store published items as a multiset of priority and value pairs.
Dequeue removes any value at the lowest published numeric priority, so
equal-priority items may be returned in any order. In MPMC, full observes
reserved plus published capacity while empty observes only published items.

Candidate code runs only in the Rust worker. A crash, hang, malformed protocol
response, invalid ABI status, or failed model check rejects the run without
placing candidate code in the Go checker process.

## Benchmark

The native runner creates producer and consumer handles once, then calls the ABI
from the producer and consumer thread counts selected by the scenario. Payload
copying, priority selection, failed try operations, and FFI transitions are
included in elapsed time. A final drain checks item-count conservation and a
commutative fingerprint of enqueued and dequeued payloads.

The benchmark command runs the correctness gate first. Repeated measurements
report the median successful enqueue-plus-dequeue rate as
`total_ops_per_sec`.

## Ownership

Files copied from `examples/evaluators/priority-queue` are trusted evaluator
inputs. The materialized `src/`, build files, and
`priority-queue-candidate.so` are candidate-owned.
