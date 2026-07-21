# Reusable Microservice Evaluator Design

## Ownership

```mermaid
flowchart LR
    W["Workload TOML"] --> C["Strict config loader"]
    C --> B["Benchmark scheduler"]
    C --> K["Accuracy runner"]
    BA["Benchmark adapter"] --> B
    AA["Independent accuracy adapter"] --> K
    AS["Mode-neutral app input support"] --> BA
    AS --> AA
    B --> R["Shared target runtime"]
    K --> R
    R --> D["Protocol driver"]
    D --> T["Running target"]
    D --> O["Common observations"]
    O --> S["Statistics and result contract"]
```

The benchmark engine owns arrival scheduling, timestamp placement, trial
lifecycle, aggregation, constraint enforcement, and output. The accuracy
runner owns managed lifecycle transitions, required-property enforcement, case
floors, and correctness result reporting. Both use the same target runtime,
protocol drivers, aggregate-deadline readiness runner, and mode-neutral
protocol preflight. Their application-specific entity and state-transition
oracles remain separate so one validator bug cannot silently bless both
qualification and scoring. Observable request encoding, authentication, fuzz
grammars, readiness ordering, and protocol-level expectations are shared so an
avoidable preamble does not reveal which evaluator mode is running.

Managed crash recovery fails closed unless the candidate can run in a dedicated
Bubblewrap PID namespace. Process groups and sampled descendant PIDs are not a
containment boundary: a daemon can change sessions before sampling, and a bare
PID can be reused. Terminating the namespace init instead gives the kernel
ownership of all descendants, including immediate double-forks.

The dependency rule is:

```text
cmd -> concrete applications and drivers
engine -> api interfaces only
accuracy runner -> api interfaces and generic validation only
engine and accuracy runner -> shared probing and sampling primitives
drivers -> api protocol payloads only
applications -> api protocol payloads only
benchmark and accuracy adapters -> mode-neutral app support, never shared oracles
stats/result -> observations only
```

Application code must not schedule workers or calculate headline metrics. A
driver must not know application operation names. The engine must not branch on
an application or protocol name.

## Timing

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

    Note over Queue,Worker: queue_wait
    Note over Worker,Service: protocol_time
    Note over Scheduler,Service: total_latency
```

For open-loop workloads, total latency begins at the scheduled arrival. Client
queueing therefore remains visible under overload. Semantic validation happens
after `completed_at`; it can invalidate a request but does not inflate protocol
latency. The separate `validated_at` timestamp bounds logical completion and is
used for achieved-throughput elapsed time.

The scheduler reports actual offered rate, scheduler lag, and maximum client
queue depth. A trial is invalid when the client cannot offer the configured
minimum fraction of target rate.

Closed-loop workloads use the same engine, drivers, observations, and semantic
validation, but each worker schedules its next logical operation after the
previous one completes. They are appropriate for saturation-throughput
objectives where a fixed open-loop rate would cap every successful candidate at
the same score. Their latency distributions are closed-loop saturation response
times; use an open-loop workload to characterize queueing under an offered rate.

## Extension points

`api.Driver` opens a target-specific `api.Client`. The client accepts an
`api.Invocation` with a protocol-specific payload and returns a
`api.ProtocolResult` that preserves native status while supplying common
transport fields. This draft implements HTTP. gRPC and Thrift should implement
the same contract and pass the same engine/driver tests rather than adding
protocol branches to the scheduler.

`api.Application` prepares fixtures, builds logical-operation plans, and
validates their collected results. A plan may contain one or more invocations;
the engine always issues and accounts for each invocation itself.
The declarative adapter covers ordinary HTTP operations. The Social Network
adapter demonstrates the typed escape hatch for dynamic users and setup.

Applications with mode-neutral startup requirements implement
`api.PreflightApplication`. The probing framework requires readiness coverage
for every configured target, transport-gates semantic validators, and executes
the same sequential protocol checks in benchmark and accuracy modes. Accuracy
applications additionally declare a framework-enforced minimum randomized case
volume; CLI bounds may increase it but cannot reduce it.

## Result validity

The evaluator emits one versioned summary and optional raw NDJSON observations.
Latency distributions include semantically successful requests; error counts
include every failed attempt. `primary_value` is omitted unless every trial:

- produced the samples required by the objective;
- sustained the configured minimum offered rate;
- satisfied success/error constraints; and
- completed without setup, execution, or interruption errors.

Individual trials are the independent aggregation units. The summary reports
their median, MAD, IQR, and a deterministic bootstrap interval when at least two
valid trials are available.
