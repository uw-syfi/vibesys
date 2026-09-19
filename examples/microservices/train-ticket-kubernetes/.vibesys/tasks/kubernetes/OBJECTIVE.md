# Train Ticket Kubernetes optimization

Maximize stateful API operations per second for the pinned Train Ticket source
on Kubernetes while preserving the v0.2.0 service contracts. The packaged
accuracy adapter checks the startup catalog, exact schemas, referential
integrity, acknowledged mutations, read-your-write behavior, and deletes.

The evaluator builds source images and deploys the config, station, train,
travel, route, and price services with their dependencies in a fresh namespace.
Every measured logical operation must pass semantic validation. Optimize the
`train-ticket` source without recognizing fixed evaluator inputs or weakening
externally observable API behavior.
