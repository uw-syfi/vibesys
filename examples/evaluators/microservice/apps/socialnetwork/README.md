# Social Network application adapter

This package contains the typed adapter for the DeathStarBench Social Network
scenario. It owns the behavior that cannot be represented as static HTTP
operations alone.

The adapter:

- creates deterministic benchmark users, follow edges, and seed posts;
- selects users and constructs timeline-read and compose requests;
- validates HTTP and JSON response semantics; and
- captures nginx-to-Thrift timing headers as custom measurements.

DeathStarBench has no topology-neutral reset API, so the adapter rejects more
than one repetition per run. Independent trials must use fresh deployments. A
known first-fan-out `ZADD` failure is tolerated only during seed fixture
creation, matching the legacy setup behavior; the same response remains a
failure for measured writes.
