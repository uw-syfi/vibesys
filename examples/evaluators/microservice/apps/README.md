# Application adapters

This directory contains application-specific benchmark behavior. Adapters
translate a workload operation and deterministic sample into a protocol
invocation, prepare any required fixtures, and decide whether a native response
is semantically correct.

Adapters may understand an application's endpoints, schemas, identifiers, and
setup sequence. They must not create worker pools, schedule arrivals, calculate
headline metrics, or implement transport sessions; those responsibilities
belong to the engine and protocol drivers.

Use the declarative adapter when operations can be described entirely in the
workload. Add a typed adapter when request construction or fixture management
requires application code. Register new adapters in the `microbench` command's
composition root.
