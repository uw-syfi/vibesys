# Social Network application adapter

This package contains the typed adapter for the DeathStarBench Social Network
scenario. It owns the behavior that cannot be represented as static HTTP
operations alone.

The adapter:

- creates seed-namespaced benchmark users, a ring of follow edges, and posts;
- selects users and constructs timeline-read and compose/read operation plans;
- validates exact post schemas, creator identity, ordering, uniqueness, the
  expected newest content window, stable post/request/timestamp identity across
  reads, compose acknowledgements, and both user- and home-timeline
  read-your-write behavior; and
- captures nginx-to-Thrift timing headers as custom measurements.

DeathStarBench has no topology-neutral reset API, so the adapter rejects more
than one repetition per run. Independent trials must use fresh deployments for
comparable state, while VibeSys optimization evaluations use a random fixture
seed independent of the deterministic load seed. Numeric IDs use aligned blocks
from the exactly representable 53-bit integer range, making collisions
negligible even across long campaigns. A known first-fan-out `ZADD` failure
is tolerated only during seed fixture creation, matching the legacy setup
behavior; the same response remains a failure for measured writes.

The `compose_post` operation remains available as a legacy alias, but uses the
same three-step semantic contract. The checked-in scenario uses
`compose_user_timeline`; both names schedule, time, and account for compose,
author timeline read, and follower home timeline read as one logical operation.
The adapter rejects `--skip-prepare` because prior live writes make an exact
state oracle impossible to reconstruct safely.
