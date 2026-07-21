# Social Network application adapter

This package contains the typed adapter for the DeathStarBench Social Network
scenario. It owns the behavior that cannot be represented as static HTTP
operations alone.

The adapter:

- creates seed-namespaced benchmark users, a ring of follow edges, and posts;
- selects users and constructs timeline-read and compose/read operation plans;
- validates exact post schemas, creator identity, ordering, uniqueness, bounded
  pagination, compose acknowledgements, and read-your-write behavior; and
- captures nginx-to-Thrift timing headers as custom measurements.

DeathStarBench has no topology-neutral reset API, so the adapter rejects more
than one repetition per run. Independent trials must use fresh deployments for
comparable state, while VibeSys optimization evaluations use random seeds so
their fixture identities cannot collide. A known first-fan-out `ZADD` failure
is tolerated only during seed fixture creation, matching the legacy setup
behavior; the same response remains a failure for measured writes.

The `compose_post` operation remains accepted for workload compatibility. The
checked-in scenario uses `compose_user_timeline`, whose compose and dependent
timeline read are scheduled, timed, and accounted for as one logical operation.
