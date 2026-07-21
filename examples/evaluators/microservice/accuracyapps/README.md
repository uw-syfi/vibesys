# Accuracy applications

Each package in this directory implements the application-specific half of an
exhaustive correctness check. An adapter declares readiness probes and required
properties, then exercises public APIs through the shared runtime.

Adapters should use the shared strict JSON contracts and fixture journal, but
must maintain an application-specific oracle independent of the corresponding
benchmark adapter. New applications should include mutation-style regression
tests proving rejection of duplicate lists, stale secondary indexes, no-op
deletes, over-deletes, lost acknowledged writes, and incomplete cleanup where
those behaviors are part of the application contract.

Every adapter must declare a case floor large enough to cover the benchmark's
state cardinality and should execute repeated mutation epochs where benchmark
traffic can revisit the same records. Seed catalogs must be verified through
their public point and secondary query paths as well as exact list membership;
a synthesized list is not proof of a usable index.

Applications without a public cleanup API, such as Hotel Reservation, must use
collision-resistant fixture namespaces and state the residual-mutation contract
explicitly. They must not register fictional cleanup callbacks or report a
property as restored when persistent state remains.
