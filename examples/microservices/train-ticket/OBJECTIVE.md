Optimize a Train Ticket v0.2.0-compatible deployment for stateful API
throughput while preserving externally observable behavior.

Headline metric: successful `operations_per_second` from the shared evaluator
result (`primary_value`, maximize). An operation may contain multiple dependent
HTTP requests; update/read and create/read/delete sequences are counted once.

The candidate may use any implementation language, process topology, database,
persistence format, or caching strategy. It must preserve the public HTTP
contract checked by `servicebench --mode accuracy`, including the startup
catalog, exact entity schemas, referential integrity, acknowledged mutations,
read-your-write, deletes, and—when a restart hook is provided—crash recovery.

Traffic values, namespaces, and operation order are randomized at evaluation
time. A run is invalid if any measured logical operation fails semantic
validation or if the load generator cannot sustain the configured offered rate.
