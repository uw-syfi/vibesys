# Application adapters

This directory contains application-specific benchmark behavior. Adapters
translate a workload operation and deterministic sample into a protocol-neutral
operation plan, prepare any required fixtures, and decide whether the collected
native responses are semantically correct.

Adapters may understand an application's endpoints, schemas, identifiers, and
setup sequence. They must not create worker pools, schedule arrivals, calculate
headline metrics, or implement transport sessions; those responsibilities
belong to the engine and protocol drivers.

Use the declarative adapter when operations can be described entirely in the
workload. Add a typed adapter when request construction or fixture management
requires application code. Register new adapters in the `servicebench` command's
composition root.

The Train Ticket adapter demonstrates dependent multi-invocation operations.
It describes update/read and create/read/delete plans, while the engine retains
exclusive ownership of issuing and measuring every HTTP request.

Accuracy adapters live separately under `accuracyapps/`. They may share the
transport, canonical wire encoding, and application input grammar, but must not
reuse a benchmark adapter's schemas or expected-value calculations. Shared
mode-neutral application support lives under `appsupport/`.
