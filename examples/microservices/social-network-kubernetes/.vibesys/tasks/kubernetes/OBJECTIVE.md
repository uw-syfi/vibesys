# Social Network Kubernetes read-timeline optimization

Minimize p50 read latency for DeathStarBench's `socialNetwork` application on
Kubernetes while preserving timeline semantics. The workload is 50% user
timeline reads, 40% home timeline reads, and 10% compose plus read-your-write
sequences.

The evaluator builds the candidate image, deploys it with the declared dependencies
in a fresh namespace, prepares seeded users and posts, and requires every
operation type to succeed. Optimize source under `deathstarbench/socialNetwork`.
Preserve ordering, visibility, pagination, identity, and read-your-write
behavior checked by the packaged workload adapter.
