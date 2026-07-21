# Accuracy framework

This package owns mechanics common to accuracy checking across microservice
applications:

- target session creation through the same drivers as benchmarking;
- aggregate-deadline readiness and complete-stop polling across every declared
  endpoint;
- fail-closed PID-namespace candidate crash/restart orchestration;
- required-property registration, fresh-evidence binding, and enforcement;
- versioned result reporting with hidden replay-seed hashes; and
- aggregate-deadline, reverse-order, retryable fixture cleanup journals that
  can take ownership before an ambiguously successful mutation is issued.

`httpcheck` strictly validates HTTP responses and exact envelopes.
`jsoncheck` validates every collection row, exact object fields, field types,
unique application-defined keys, and exact expected collection membership.
These primitives default to rejection so new accuracy adapters do not
accidentally accept duplicate, partial, stale, unexpected, or malformed
collections.

Application endpoint mappings, seed oracles, entity relationships, and state
transitions do not belong here. They live in `accuracyapps/` and remain
independent of benchmark application validation.
