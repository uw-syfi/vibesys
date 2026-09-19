# Queue Evaluator Design

This document describes the v1 architecture for the bounded queue tasks:
`spsc`, `mpsc`, and `mpmc` in the local `queue-rs` example repository. It
explains the trust boundary and the split between VibeSys, the versioned
evaluator package, the Rust native runner, and the candidate shared library.

The exact candidate function contract is specified separately in
[`CANDIDATE_CONTRACT.md`](CANDIDATE_CONTRACT.md).

## Goals

- Let a candidate use any language that can export a C ABI.
- Keep candidate lifetime and ownership requirements simple and explicit.
- Check each scenario's bounded FIFO history semantics without trusting
  candidate-generated histories, timestamps, values, or verdicts.
- Contain ordinary candidate crashes during correctness checking.
- Measure native queue throughput without per-operation IPC or language-runtime
  overhead in the timed path.
- Keep benchmark configuration, timing, counters, payload validation, and score
  extraction in evaluator-owned code.
- Give every bounded queue input the same editable but untrusted baseline.

## Non-goals

- Protect the host from actively malicious native code. The worker process is a
  fault boundary, not an OS security sandbox.
- Isolate the benchmark from a hostile candidate in the same address space.
- Define a zero-copy ownership or buffer-allocation contract.
- Support lossy or batched queue semantics in ABI v1.
- Exhaustively prove correctness for unbounded histories. Linearizability
  checking has exponential worst-case behavior, so v1 checks many bounded,
  independently seeded histories.

## Components

```text
VibeSys agent loop
  |
  | launches from the queue-rs repository root
  | resolves and digest-verifies the immutable evaluator package
  | dispatches task accuracy and benchmark entry points
  v
Go queue evaluator
  |-- correctness control, histories, Porcupine model and verdict
  |-- benchmark repetition and result validation
  |
  | correctness: Unix socket lanes       benchmark: process invocation
  v                                      v
Rust native runner worker             Rust native benchmark runner
  |                                      |
  | copying C ABI                        | copying C ABI on timed threads
  v                                      v
queue-candidate.so                    queue-candidate.so
```

The source is organized as follows:

| Path | Responsibility |
| --- | --- |
| `*.go` | CLI, candidate process control, histories, Porcupine checking, benchmark aggregation, evaluator result stream |
| `native_runner/src/abi.rs` | Dynamic loading and typed ownership of the candidate C ABI |
| `native_runner/src/protocol.rs` | Correctness worker and per-lane socket protocol |
| `native_runner/src/probe.rs` | ABI edge-case and copying-lifetime probes |
| `native_runner/src/benchmark.rs` | Native timed producer and consumer threads |
| `include/vibesys_queue_abi.h` | Candidate ABI declarations |
| `examples/data-structures/repositories/queue-rs` | Local repository-shaped untrusted Rust candidate |

Go owns the correctness model because Porcupine is a mature Go implementation.
Rust owns all candidate FFI so C ABI loading, native handle lifetimes, and direct
benchmark calls are implemented once. The candidate never interacts directly
with Go.

VibeSys copies this package to `_evaluator/<name>` in the candidate workspace
before running it, so it must build from a relocated tree with no path back to
this repository. Every Go dependency, including the evaluator SDK
(`github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval`), is an ordinary published
module requirement resolved from the module proxy: no relative `replace`, no
committed `vendor/`. To change the SDK and this evaluator together, use a
developer-local Go workspace, described in [sdk/README.md](../../../sdk/README.md).

## Repository and Evaluator Integrity

The three task manifests live under the candidate repository:

```text
examples/data-structures/repositories/queue-rs/
├── src/
└── .vibesys/tasks/{spsc,mpsc,mpmc}/
```

Each manifest selects an exact evaluator package and its logical entry point:

```toml
[evaluator]
name = "vibesys-evaluator-queue"
version = "0.1.0"

[accuracy]
entrypoint = "vibesys-queue"
args = ["check", "--workspace", "${PROJECT_ROOT}", "--scenario", "spsc"]
```

The repository implementation, `Cargo.toml`, `Makefile`, and generated
`queue-candidate.so` are candidate-owned and may be replaced by the agent. The
task definitions under `.vibesys/tasks/` are trusted and read-only to agents.

VibeSys resolves the exact package from its installed evaluator resources,
computes a content digest, and resolves the logical entry point to a direct
command. The selected run environment expands `${PROJECT_ROOT}` to the candidate
repository root and mounts the immutable package read-only when isolation is in
use. This remains correct even though the package command uses `go -C` to run
from evaluator source.

This Git comparison protects against ordinary agent edits to evaluator files.
It is not a substitute for an OS sandbox against native code that can access
other processes or the filesystem.

## Candidate ABI

The candidate exports queue, producer, and consumer lifecycle functions plus
nonblocking `try_enqueue` and `try_dequeue` operations. Producer and consumer
handles are each confined to one native thread; handles belonging to different
threads operate concurrently on the same queue.

ABI v1 copies values:

- enqueue input storage is valid only until the call returns;
- dequeue writes into caller-owned output storage;
- the candidate may not retain either pointer;
- queue capacity is measured in items;
- full and empty results are observable operations;
- undersized dequeue output must leave the oldest value queued for retry.

Copying is intentional. A zero-copy API would need to prescribe allocation,
ownership transfer, reclamation, and buffer lifetime rules. Those choices would
exclude otherwise valid queue implementations and make the trusted driver part
of the candidate memory-management design. A future zero-copy contract should
therefore use a new ABI version rather than extending v1 implicitly.

## Correctness Flow

The manifest accuracy command starts the Go evaluator. For each selected
scenario the evaluator performs these steps:

1. Validate scenario, capacity, value size, worker counts, and candidate path.
2. Run isolated ABI probes for the configured profile, a 257-byte profile, and
   a 1 MiB profile.
3. Run deterministic boundary histories at configured capacity and capacities
   1, 2, and 3.
4. Run independently seeded concurrent histories with the scenario's producer
   and consumer counts.
5. Convert the recorded operations into a Porcupine history and check the
   bounded FIFO model within a per-history time budget.
6. Optionally write the first rejected or undecided history as JSON for
   reproduction.

### ABI Probes

The Rust `probe` command loads the candidate in a short-lived process. It tests
actual value lengths including 0, 1, 7, 8, 9, intermediate lengths, 257 bytes,
and 1 MiB. It also tests:

- producer-buffer reuse immediately after enqueue;
- unchanged output on empty dequeue;
- unchanged output and retained queue value after an undersized dequeue;
- valid queue and handle lifecycle behavior;
- status and output-length validation.

MPMC profiles additionally race two consumer handles against one producer with
alternating 16-byte and 64-byte values. One consumer has only 32 bytes of
output space while the other can accept every value. The probe checks that
concurrent `INVALID`/`EMPTY` results leave caller storage untouched and that
all successfully enqueued values are returned exactly once without corruption.

Each probe process can fail without corrupting Go-owned checker state. Probe
processes have a trusted wall-clock deadline.

### Boundary Histories

A boundary history uses one mixed producer/consumer lane and exercises:

- dequeue from empty;
- fill exactly to capacity;
- enqueue while full;
- partial drain;
- refill to force wraparound behavior;
- full observation after refill;
- complete FIFO drain;
- final empty observation.

The observations are checked with the same Porcupine model as concurrent
histories rather than with a second queue specification.

### Concurrent Histories

Go creates one client goroutine and one socket lane for each producer or
consumer. The Rust worker creates one native thread and one candidate handle per
lane. A shared atomic logical clock records a call event immediately before Go
sends a request and a return event immediately after Go receives its response.

Go, not the candidate, owns:

- unique enqueue values and deterministic payload bytes;
- client identities and operation selection;
- call and return timestamps;
- response decoding and payload validation;
- the complete history and Porcupine verdict.

Histories are limited to 32 approximate operations. Increasing the number of
trials provides more schedules without making one Porcupine search intractable.

### Check Budget

Porcupine's search is worst-case exponential in the number of overlapping
operations, so history size alone does not bound the work. Real histories decide
in milliseconds, but some interleavings of a wide producer/consumer history run
for minutes.

Every history is therefore decided with `CheckOperationsTimeout` under a budget,
default 20 seconds per history and overridable with `--check-budget` on both
`check` and `benchmark`. The verdict is tri-state:

| Verdict | Meaning | Gate outcome |
| --- | --- | --- |
| `Ok` | A legal linearization exists | Pass |
| `Illegal` | No legal linearization exists | Fail as a contract violation |
| `Unknown` | The budget expired first | Fail as an undecided history |

An undecided history is a failure, not a pass. The checker is the candidate's
adversary, so folding `Unknown` into `Ok` would let a candidate win by producing
histories the checker cannot decide. The failure names the history, the
scenario, and the budget, so a task can raise `--check-budget` or narrow the
worker counts deliberately.

Zero is rejected rather than passed through: Porcupine reads a zero timeout as
unlimited, which is the unbounded search the budget exists to prevent.

### Queue Models

SPSC and MPSC use the exact bounded FIFO model. Its Porcupine state is a slice
of published values:

- a successful enqueue is legal only below capacity and appends its value;
- a full enqueue is legal only at capacity and leaves state unchanged;
- a successful dequeue must return and remove the oldest value;
- an empty dequeue is legal only when the state is empty.

MPMC uses a reservation-aware model with two state components: unpublished
reservations identified by enqueue operation and the FIFO sequence of published
values. Keeping reservation identity separate from payload contents permits
multiple outstanding enqueues of the same value. Before checking, each
successful enqueue is expanded into a reservation event followed by a
publication event. Both events retain the original operation's call/return
interval, and the model requires reservation before publication.

- reservation is legal only while reserved plus published items are below
  capacity;
- publication moves the reservation's value to the FIFO tail;
- full is legal when reserved plus published items equal capacity;
- a successful dequeue removes the oldest published value;
- empty is legal when the published FIFO is empty, regardless of reservations.

Porcupine searches for event orderings that satisfy the selected model and the
real-time precedence implied by non-overlapping call/return intervals. The MPMC
expansion captures the two externally relevant points of reserve-copy-publish
queues without exposing implementation-specific state through the ABI.

## Correctness Worker Protocol

Correctness uses inherited Unix `SOCK_STREAM` socket pairs. File descriptors
start at 3 in the Rust worker. There is normally one lane per producer or
consumer; deterministic boundary checking uses one mixed lane.

Every request and response starts with a 16-byte little-endian header:

| Offset | Width | Meaning |
| --- | ---: | --- |
| 0 | 4 | Operation or response status |
| 4 | 4 | Payload length |
| 8 | 8 | Reserved, required to be zero |

An enqueue request is followed by its generated payload. A successful dequeue
response is followed by the returned payload. Other operations and statuses
carry no payload. Both implementations reject unknown operations, statuses,
nonzero reserved fields, oversized lengths, and unexpected payloads.

Each lane permits one outstanding request, but all lanes operate concurrently.
The per-lane round trip is acceptable because this protocol is used only for
correctness. It is deliberately absent from the benchmark hot path.

Closing all Go-side lanes tells the worker to finish. Early worker exit, broken
sockets, malformed frames, invalid ABI statuses, and candidate crashes all
become checker failures with bounded worker logs attached.
Each correctness request also has a trusted deadline; a candidate operation
that does not return is terminated and reported as a timeout rather than
hanging the checker.

## Benchmark Flow

The Go benchmark command first runs a reduced correctness gate. It does not
benchmark a candidate that fails ABI probes, boundary checks, or its concurrent
history, and it does not benchmark one whose history cannot be decided within
the check budget. The gate decides five histories (four boundary, one
concurrent), so the default budget keeps it inside the framework's benchmark
timeout with room for the measured run. A gate failure reaches the framework as
an evaluator error record carrying that reason.

For each requested repetition, Go starts the Rust `benchmark` command. Rust
loads the candidate and calls the C ABI directly from native producer and
consumer threads:

1. Create the queue and one handle per measured thread.
2. Synchronize workers on a barrier.
3. Run an optional warmup phase.
4. Run the measured phase, checking the clock every 64 attempts.
5. Count successful enqueue/dequeue and full/empty observations locally.
6. Validate every dequeued payload.
7. Stop all threads, drain the queue, and validate conservation.
8. Write evaluator-owned JSON for Go to validate and aggregate.

Each native repetition has a deadline derived from its configured warmup and
measured duration plus a fixed shutdown grace, so a candidate that deadlocks
during the timed phase or final drain cannot hang the outer evaluator.

Two keyed commutative fingerprints compare the multiset of successfully
enqueued values with values dequeued during measurement and final drain. Counts
also require:

```text
successful enqueues = measured dequeues + final drained values
```

These checks catch loss, duplication, fabrication, and payload corruption
without serializing the timed operations through a trusted recorder. They do
not prove FIFO linearizability; that is the preceding correctness gate's job.

Go rejects unknown JSON fields, mismatched scenarios or worker counts, and
inconsistent attempt totals. An odd number of repetitions is required, and
`total_ops_per_sec` is taken from the median repetition while all samples are
retained.

### Benchmark Output

The benchmark command writes two independent outputs, and both flags are
optional.

`--vs-output` names the VibeSys evaluator record stream, the framework-facing
result channel specified by `sdk/vs-evaluator/PROTOCOL.md`. The command declares
one required metric, `total_ops_per_sec` in `ops/s` with direction `max`. The
output opens as soon as the argv parses, and the hello record is written before
the first repetition, so a rejected invocation reaches the framework as a reason
and a crashed or timed-out run still leaves a stream that names its metric. The
stream then closes with either a result record carrying the median rate or an
error record naming why no rate exists. Protocol 2 carries one row, so a run
that reports the stream measures a single `--scenario`; `--scenario all` with
`--vs-output` is rejected before any measurement.

`--output-json` keeps the detailed report: per-scenario counters, duration,
worker counts, and every repetition sample. It is human debugging output, not
the metric channel, and it still accepts `--scenario all`.

Omitting `--vs-output` reports nothing to the framework and leaves the printed
summary and the detailed report unchanged. Standalone baseline invocations use
that form.

`[benchmark.result]` in a task manifest selects the `--output-json` path
instead: VibeSys runs the immutable benchmark command and accepts only one
finite numeric field with the declared name. SPSC, MPSC, and MPMC declare
`total_ops_per_sec`.

## Trust Model

| Component | Status | Rationale |
| --- | --- | --- |
| VibeSys framework gate | Trusted | Resolves exact evaluator packages, verifies digests, and extracts the score |
| Go evaluator and Porcupine | Trusted | Own histories, queue model, correctness verdict, repetitions, and result validation |
| Rust native runner | Trusted | Owns C ABI adaptation, probes, timed threads, counters, payload checks, and output JSON |
| ABI document/header | Trusted contract | Defines the candidate behavior being evaluated |
| `queue-rs` source and build files | Untrusted | Initial candidate only; agents may replace them |
| `queue-candidate.so` | Untrusted | Subject of correctness and performance evaluation |
| Agent verdicts and metrics | Untrusted | Framework gates replace them when manifest contracts are configured |
| OS, process APIs, Go/Rust toolchains | Assumed trusted | v1 does not attempt to verify the execution platform |

Correctness process separation prevents a normal segmentation fault or memory
corruption in the candidate from directly corrupting Go's history. It does not
stop hostile native code from using OS facilities to attack the parent checker.

The performance runner and candidate intentionally share an address space. This
removes IPC and extra FFI layers from measured operations, but a hostile
candidate could corrupt counters, output, or control flow. V1 performance
scoring therefore assumes a cooperative candidate. WASM, software fault
isolation, or another same-address-space isolation mechanism is a possible v2
extension.

## Failure Handling and Limits

- Missing libraries, symbols, or ABI-version mismatches fail before workloads.
- Invalid status codes, malformed protocol frames, payload corruption, and
  worker crashes fail evaluation.
- Worker output is capped at 64 KiB before inclusion in an error.
- A rejected linearizability history can be persisted for reproduction.
- Linearizability checking is bounded per history; an undecided history fails
  the gate with the scenario and budget in its reason rather than hanging until
  the surrounding command timeout kills the run.
- The evaluator does not currently impose its own per-operation deadline. Command
  timeout enforcement belongs to the surrounding execution layer.
- Correctness schedules are sampled, not exhaustive.
- Fingerprints make benchmark conservation validation probabilistic.
- Performance results are meaningful only when the candidate and execution
  environment cooperate with the trusted runner.

## Validation

The main checks for this design are:

- `go test -race ./...` in `resources/evaluators/queue`;
- Rust unit tests, formatting, and Clippy in `native_runner`;
- Rust formatting and Clippy for the `queue-rs` example repository;
- `tests/examples/test_queue_evaluator.py` for manifest, package resolution, build,
  correctness, benchmark, and adversarial-history integration;
- `tests/loops/agent/test_orchestrate.py` for framework accuracy and benchmark
  gate behavior.

Changes to the ABI, worker protocol, trust model, scoring path, or isolation
assumptions should update this document in the same pull request.
