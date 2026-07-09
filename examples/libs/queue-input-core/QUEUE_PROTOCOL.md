# VibeServe Queue Candidate Protocol v1

The trusted queue harness and the candidate run in separate processes. The
harness owns the workload, timestamps, operation history, linearizability
model, Porcupine invocation, benchmark clock, and reported counters. The
candidate owns only the queue implementation and the adapter that serves queue
operations.

This boundary supports candidates implemented in any language that can map a
file and perform aligned 64-bit atomic loads and stores. It intentionally does
not define a Python class or language-specific ABI.

## Launcher contract

The candidate workspace must contain an executable file named
`queue-candidate`. The harness invokes it directly as:

```text
./queue-candidate --vibeserve-queue-shm <path>
```

The launcher may compile code, select a runtime, and then replace itself with
the candidate server. Scenario, capacity, and client-lane count come from the
mapped header, not from candidate-reported configuration.

The harness waits for the ready field before issuing operations. On completion
it sets the stop field. Process-level timeout policy is outside this protocol.

## Encoding

All integers are little-endian. The shared-memory file starts with a 4096-byte
header followed by one 4096-byte lane per logical client. Every lane contains
an SPSC request ring and an SPSC response ring. One trusted worker publishes
requests and one candidate worker publishes responses. Correctness checks keep
one operation in flight per lane; benchmarks may pipeline up to the advertised
ring size to amortize synchronization overhead.

Header fields:

| Offset | Size | Field |
|---:|---:|---|
| 0 | 8 | ASCII magic `VSQUEUE1` |
| 8 | 4 | Protocol version, currently `1` |
| 12 | 4 | Lane count |
| 16 | 8 | Queue capacity |
| 24 | 4 | Scenario: `1` SPSC, `2` MPSC, `3` MPMC |
| 28 | 4 | Ring slots, currently `64` |
| 32 | 8 | Candidate-ready flag |
| 40 | 8 | Harness-stop flag |

Each lane starts at `4096 + lane_index * 4096`:

| Lane offset | Size | Field |
|---:|---:|---|
| 0 | 8 | Last request sequence published by the harness |
| 64 | 8 | Last request sequence consumed by the candidate |
| 128 | 8 | Last response sequence published by the candidate |
| 192 | 8 | Last response sequence consumed by the harness |
| 256 | 1024 | 64 request slots of 16 bytes each |
| 1280 | 1024 | 64 response slots of 16 bytes each |

A request slot contains a 4-byte operation at offset 0 and an 8-byte enqueue
value at offset 8. A response slot contains a 4-byte status at offset 0 and an
8-byte dequeue value at offset 8. Sequence `n` uses slot `(n - 1) % 64`.

Operations are `1` for enqueue and `2` for dequeue. Response statuses are:

| Status | Meaning |
|---:|---|
| 1 | Enqueue succeeded |
| 2 | Enqueue observed a full queue |
| 3 | Dequeue returned the value at lane offset 80 |
| 4 | Dequeue observed an empty queue |
| 5 | Candidate protocol error |

## Memory ordering

Payload fields are written before the corresponding published-sequence field.
A publisher releases available slots by atomically storing the newest sequence
with release-or-stronger ordering. A reader atomically loads that field with
acquire-or-stronger ordering, processes slots in sequence order, and publishes
the last consumed sequence. A publisher must not advance more than 64 entries
past the consumed sequence. The ready and stop fields are also aligned atomic
64-bit values with acquire/release-or-stronger ordering.

Each candidate lane worker retains its last handled request sequence and must
respond exactly once to every new sequence. It should stop after observing the
stop flag and no unhandled request.

## Correctness and measurement

The checker supplies unique unsigned 64-bit enqueue values. It records calls
immediately before publishing a request and returns immediately after observing
the matching response. Candidate memory cannot change the harness's private
copy of inputs, timestamps, histories, or counters.

The accuracy checker runs deterministic empty/full/wraparound probes followed
by independently seeded concurrent histories at the configured capacity and at
small capacities that force contention on full/empty transitions. Benchmarking
first runs a short
linearizability gate, then measures successful protocol operations during
trusted warmup and measurement intervals. After each phase, the harness drains
the queue and compares two privately keyed fingerprints of successful enqueues
and returned values. This detects loss, fabrication, and duplication without
trusting candidate counters or retaining the full benchmark history. Candidate
output is diagnostic only and is never used as a verdict or performance result.

The Go reference server is in
`src/queue_input_core/trusted_harness/candidate.go`. It is also available via
the evaluator's `--use-reference` option. Native candidates can use the C11
layout and constants in `include/vibeserve_queue_protocol.h`.

For scored execution, the evaluator and this protocol definition must be
provided from a trusted, immutable input copy. A convenience copy inside an
agent-writable workspace is not itself an isolation boundary.
