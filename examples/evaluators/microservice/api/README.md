# API contracts

This package defines the stable contracts shared by the evaluator core and its
extensions. It contains no concrete application, protocol, scheduling, or
statistics implementation.

The main contracts are:

- `Workload`, `Target`, and `Operation`, which represent the resolved benchmark
  configuration;
- `Driver` and `Client`, which isolate protocol sessions and invocations;
- `Application`, which owns fixture lifecycle, request construction, and
  semantic response validation;
- `AccuracyApplication`, which owns an independent exhaustive application
  oracle while the shared runner enforces readiness and required properties;
- `PreflightApplication`, which exposes mode-neutral readiness and protocol
  probes shared by benchmark and accuracy execution;
- `OperationPlan`, `Invocation`, and `ProtocolResult`, which let one scheduled
  logical operation contain one or more engine-accounted protocol calls; and
- `Observation`, which records common logical-operation outcome and timing
  fields together with physical invocation counts.

Protocol-specific data belongs in an invocation payload or protocol result.
Application-specific data belongs in the application adapter. Adding either to
the scheduler would break the dependency boundary this package exists to
enforce.
