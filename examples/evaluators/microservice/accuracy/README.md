# Accuracy framework

This package owns mechanics common to accuracy checking across microservice
applications:

- target session creation through the same drivers as benchmarking;
- shared aggregate-deadline readiness, protocol preflight, and complete-stop
  polling across every declared endpoint;
- fail-closed PID-namespace candidate crash/restart orchestration;
- required-property registration, fresh-evidence binding, and enforcement;
- versioned result reporting with hidden replay-seed hashes;
- application-declared case floors and hidden randomized extra cases that CLI
  flags cannot weaken; and
- aggregate-deadline, reverse-order, retryable fixture cleanup journals that
  can take ownership before an ambiguously successful mutation is issued.

`httpcheck` strictly validates HTTP responses and exact envelopes, including a
string message field and mathematically integral status values regardless of
equivalent JSON spelling.
`jsoncheck` validates every collection row, exact object fields, field types,
unique application-defined keys, and exact expected collection membership.
JSON numeric equality is mathematical, so representation-only differences such
as `1`, `1.0`, and `1e0` do not couple the oracle to one serializer.
These primitives default to rejection so new accuracy adapters do not
accidentally accept duplicate, partial, stale, unexpected, or malformed
collections.

Application endpoint mappings, seed oracles, entity relationships, and state
transitions do not belong here. They live in `accuracyapps/` and remain
independent of benchmark application validation.

The runner rejects readiness declarations that omit or invent workload targets
and transport-gates every semantic readiness validator. Registry composition
also requires a factory's application identity to match its workload key.
