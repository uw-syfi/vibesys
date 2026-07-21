# Microservice Benchmark Evaluator

This directory contains the trusted, reusable benchmark evaluator for online
microservice applications. Scenario bundles provide a strict TOML workload and
select an application adapter; the evaluator supplies consistent load
generation, protocol execution, measurement, correctness gating, statistics,
and output across applications.

The initial implementation includes a production HTTP driver and adapters for
Train Ticket and DeathStarBench Social Network. The same core is designed to
support future gRPC and Thrift drivers without adding protocol-specific logic to
the scheduler.

## Design overview

The system separates four kinds of concern:

| Layer | Owns | Does not own |
| --- | --- | --- |
| Workload | Targets, traffic mix, load, objective, and validity constraints | Scheduling or protocol code |
| Core engine | Trial lifecycle, open- or closed-loop scheduling, timestamps, aggregation, and result validity | Application schemas or wire formats |
| Application adapter | Fixture setup, dynamic request construction, and semantic response validation | Worker scheduling or headline statistics |
| Protocol driver | Connections, serialization, transport calls, and native response metadata | Application operation meaning |

```mermaid
flowchart LR
    W["Workload"] --> E["Shared benchmark core"]
    A["Application adapter"] <--> E
    E <--> D["Protocol driver"]
    D <--> T["Running application"]
    E --> R["Validated results"]
```

The dependency direction is deliberate:

```text
cmd -> concrete applications and drivers
engine -> api interfaces only
drivers -> api protocol payloads only
applications -> api protocol payloads only
statistics/results -> common observations only
```

The engine never branches on an application or protocol name. A new protocol
implements `api.Driver` and `api.Client`; a new application implements
`api.Application`. The command registers concrete implementations at startup,
and the workload selects them by name.

When an application implements `api.PreflightApplication`, benchmark and
accuracy execution use the same readiness and protocol-probe plans before
mode-specific work. Accuracy applications also declare an unskippable minimum
case volume and randomized extra range, so low CLI bounds or one fixed fixture
count cannot silently weaken capacity coverage.

### Trial lifecycle

Each repetition is an independent aggregation unit. The application gets a
chance to reset and prepare fixtures before the engine runs an unmeasured
warmup followed by the measured phase.

```mermaid
sequenceDiagram
    participant CLI as servicebench
    participant App as Application adapter
    participant Engine as Trial engine
    participant Driver as Protocol driver
    participant Target as Running target

    CLI->>Engine: Run(resolved workload)
    loop Each repetition
        Engine->>App: Reset(trial, seed)
        opt Fixture preparation enabled
            Engine->>App: Prepare(trial, seed)
        end
        opt Configured warmup
            Engine->>Driver: Generate warmup traffic
            Driver->>Target: Invoke
        end
        loop Scheduled measured arrivals
            Engine->>App: BuildOperation(operation, sample)
            App-->>Engine: OperationPlan
            loop Every invocation in the plan
                Engine->>Driver: Invoke(invocation)
                Driver->>Target: Protocol request
                Target-->>Driver: Native response
                Driver-->>Engine: ProtocolResult
            end
            Engine->>App: ValidateOperation(operation, plan, results)
            App-->>Engine: Semantic result + custom timings
        end
        Engine->>Engine: Summarize trial and enforce constraints
    end
    Engine-->>CLI: Aggregate result
```

The workload seed controls operation selection and adapter samples. A separate
fixture seed controls persistent fixture namespaces, so repeated evaluations
can isolate state while preserving an identical request schedule. Each trial
derives deterministic load and fixture seeds, and both resolved seeds plus the
canonical workload hash are recorded in the result.

### Open-loop timing model

Requests are scheduled at fixed intervals independently of completion time.
Workers consume those arrivals through a bounded queue. This preserves overload
behavior: if the target or client cannot keep up, the delay appears in the
measurement instead of being hidden by a closed request loop.

```mermaid
sequenceDiagram
    participant Scheduler
    participant Queue
    participant Worker
    participant Service

    Scheduler->>Queue: scheduled_at
    Queue->>Worker: dispatched_at
    Worker->>Service: sent_at
    Service-->>Worker: completed_at

    Note over Queue,Worker: queue_wait = dispatched_at - scheduled_at
    Note over Worker,Service: protocol_time = completed_at - sent_at
    Note over Scheduler,Service: total_latency = completed_at - scheduled_at
```

Application validation runs after `completed_at`. It can reject a response but
does not inflate the recorded protocol latency. `validated_at` records when
semantic validation finishes; achieved-throughput elapsed time includes that
tail so validator work cannot inflate the primary metric. The engine also
reports actual offered rate, scheduler lag, and maximum queue depth so a
benchmark can distinguish target behavior from load-generator saturation.

### Closed-loop saturation model

Closed-loop workloads keep the configured number of logical operations in
flight for the measurement duration. Each worker schedules its next operation
only after the previous operation completes. This measures achieved throughput
without imposing a fixed offered-rate ceiling. Queue wait and scheduler lag are
zero by construction; total latency still spans every invocation in the logical
operation. These are saturation response-time samples, not open-loop latency:
they intentionally do not model arrivals queueing during a target stall.

### Correctness and result validity

Transport success is not sufficient. The application adapter validates native
status and application-level semantics—for example, an HTTP 200 containing a
Train Ticket error envelope is still a failed request.

Latency distributions contain only semantically successful logical operations.
Error counts contain every failed operation. Physical invocation counts and
bytes remain visible separately. A trial is invalid when it:

- has no successful samples matching the objective;
- fails to sustain the workload's minimum offered-rate ratio;
- violates a success-rate, error-rate, or per-operation coverage constraint; or
- fails during reset, setup, execution, or interruption.

An invalid run omits `primary_value`, preventing the optimization loop from
treating a fast-but-incorrect or client-limited result as an improvement.

The summary aggregates trial-level primary values rather than pooling every
operation across trials. It reports median, median absolute deviation (MAD), and
interquartile range (IQR), plus a deterministic bootstrap confidence interval
when at least two valid trials are available. The optional raw NDJSON output
retains one observation per measured logical operation for diagnosis.

## Package map

| Directory | Responsibility |
| --- | --- |
| [`api/`](api/) | Shared workload, extension, and observation contracts |
| [`accuracy/`](accuracy/) | Accuracy orchestration and fail-closed validation primitives |
| [`accuracyapps/`](accuracyapps/) | Independent application-specific accuracy oracles |
| [`appsupport/`](appsupport/) | Mode-neutral topology, preflight, input, and authentication grammars |
| [`apps/`](apps/) | Application-adapter extension layer |
| [`apps/declarative/`](apps/declarative/) | Declarative HTTP request and response adapter |
| [`apps/hotel/`](apps/hotel/) | Typed DeathStarBench Hotel Reservation adapter |
| [`apps/socialnetwork/`](apps/socialnetwork/) | Typed DeathStarBench Social Network adapter |
| [`cmd/`](cmd/) | Executable composition roots |
| [`cmd/servicebench/`](cmd/servicebench/) | Benchmark and accuracy CLI |
| [`config/`](config/) | Strict TOML decoding, defaults, profiles, and canonical serialization |
| [`drivers/`](drivers/) | Protocol-driver extension layer |
| [`drivers/httpdriver/`](drivers/httpdriver/) | HTTP transport and connection policy |
| [`engine/`](engine/) | Scheduler, trial lifecycle, statistics, and results |
| [`probing/`](probing/) | Shared readiness and protocol-preflight execution |
| [`registry/`](registry/) | Driver and application registration |
| [`sampling/`](sampling/) | Shared deterministic case-volume sampling |
| [`transport/`](transport/) | Shared target runtime used by benchmark and accuracy modes |
| [`wire/httpjson/`](wire/httpjson/) | Canonical JSON-over-HTTP request construction |

Every directory has its own README with its ownership boundary and extension
guidance.

More detailed ownership rules and measurement semantics are recorded in
[`DESIGN.md`](DESIGN.md).

## Workload model

A workload declares:

- one application adapter;
- one or more named protocol targets;
- weighted operations and optional objective tags;
- open- or closed-loop model, rate, duration, warmup, concurrency, timeout,
  load seed, fixture seed, and repetitions;
- the primary metric and direction; and
- correctness and offered-load constraints.

Unknown TOML fields are rejected. Target session policy is explicit: the HTTP
driver supports connection `reuse` and `new_per_request`. Application-specific
configuration is accepted only by the selected adapter.

See the checked-in workloads for complete examples:

- `../../microservices/train-ticket/benchmark/workload.toml`
- `../../microservices/social-network-read-timeline/benchmark/workload.toml`
- `../../microservices/hotel-reservation/benchmark/workload.toml`

## Running the evaluator

From the repository root:

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --base-url http://localhost:8080 \
  --output-json /tmp/result.json \
  --output-raw /tmp/requests.ndjson
```

Validate a workload and its registered extensions without running traffic:

```bash
go -C examples/evaluators/microservice run ./cmd/servicebench \
  --workload "$PWD/examples/microservices/train-ticket/benchmark/workload.toml" \
  --validate-only
```

Use `go run ./cmd/servicebench --help` from this directory for target, load,
profile, fixture, and output overrides.

Managed accuracy and benchmark runs may pair `--run-command-json` with
`--stop-command-json` and `--cleanup-command-json`. The stop command runs from
the candidate directory whenever the contained process is stopped, including
accuracy restarts. The cleanup command runs once when the managed lifecycle
closes, allowing an external supervisor such as Docker Compose to remove the
remaining project resources after restart-sensitive checks are complete.

## Testing

```bash
go test -race ./...
go vet ./...
```

Tests use fake applications and drivers to verify that scheduling and
aggregation stay protocol-neutral, and HTTP test servers to verify request,
response, timeout, and connection-reuse behavior.
