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
