# VibeServe Queue Candidate Protocol v1

The trusted queue harness and the candidate run in separate processes. The
harness owns the workload, timestamps, operation history, linearizability
model, Porcupine invocation, benchmark clock, and reported counters. The
candidate owns the queue implementation and the adapter that serves queue
operations.

This boundary supports candidates implemented in any language that can use
inherited Unix stream sockets. It intentionally does not define a Python class
or language-specific ABI.

## Launcher contract

The candidate workspace must contain an executable file named
`queue-candidate`. The harness creates one full-duplex Unix socketpair per
logical client and invokes the launcher directly as:

```text
./queue-candidate \
  --vibeserve-queue-protocol 1 \
  --vibeserve-queue-fd-base 3 \
  --vibeserve-queue-lanes <count> \
  --vibeserve-queue-capacity <capacity> \
  --vibeserve-queue-scenario <spsc|mpsc|mpmc>
```

The candidate endpoints are inherited as consecutive descriptors beginning at
the given base. Descriptor `fd_base + i` is lane `i`. A launcher may compile
code, select a runtime, and then replace itself with the candidate server, but
it must preserve those descriptors.

The sockets are ready when the candidate starts, so there is no separate
handshake. The harness closes its endpoints after the workload. The candidate
must finish pending writes, close its endpoints, and exit after observing EOF
on every lane. Process-level timeout policy is outside this protocol.

## Frames

Each lane is an ordered byte stream. Requests and responses are fixed 16-byte,
little-endian frames:

| Offset | Size | Request | Response |
|---:|---:|---|---|
| 0 | 4 | Operation | Status |
| 4 | 4 | Reserved, must be zero | Reserved, must be zero |
| 8 | 8 | Enqueue value | Dequeue value |

Operations are `1` for enqueue and `2` for dequeue. Response statuses are:

| Status | Meaning |
|---:|---|
| 1 | Enqueue succeeded |
| 2 | Enqueue observed a full queue |
| 3 | Dequeue returned the response value |
| 4 | Dequeue observed an empty queue |
| 5 | Candidate protocol error |

Stream reads and writes may split or combine frames. Both sides must continue
until all 16 bytes of each frame have been transferred. The candidate must
return exactly one response per request and preserve request order within each
lane. Operations on different lanes may complete in any order. The operation
field is authoritative; a lane is not permanently restricted to enqueue or
dequeue operations.

Candidates should service lanes concurrently against the same queue instance.
The trusted checker keeps one operation outstanding per lane so its call and
return boundaries remain precise. The benchmark keeps a rolling window of up
to 64 outstanding requests per lane: whenever an ordered response frees a
slot, the harness sends another request until the measurement deadline, then
drains the remaining responses. This transport pipeline does not add batch
operations to the queue API.

## Correctness and measurement

The checker supplies unique unsigned 64-bit enqueue values. It records calls
immediately before sending a request and returns immediately after receiving
the matching response. The candidate cannot change the harness's private copy
of inputs, timestamps, histories, or counters.

The accuracy checker runs deterministic empty/full/wraparound probes followed
by independently seeded concurrent histories at the configured capacity and at
small capacities that force contention on full/empty transitions. Benchmarking
first runs a short linearizability gate, then measures successful end-to-end
protocol operations during trusted warmup and measurement intervals. After
each phase, the harness drains the queue and compares two privately keyed
fingerprints of successful enqueues and returned values. This detects loss,
fabrication, and duplication without trusting candidate counters or retaining
the full benchmark history. Candidate output is diagnostic only and is never
used as a verdict or performance result.

The Go reference server is in
`src/queue_input_core/trusted_harness/candidate.go`. It is also available via
the evaluator's `--use-reference` option. Candidate implementations should use
the frame encoding defined in this document as the canonical wire contract.

For scored execution, the evaluator and this protocol definition must be
provided from a trusted, immutable input copy. A convenience copy inside an
agent-writable workspace is not itself an isolation boundary.
