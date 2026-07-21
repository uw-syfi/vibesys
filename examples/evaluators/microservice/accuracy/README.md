# Accuracy framework

This package owns mechanics common to accuracy checking across microservice
applications:

- target session creation through the same drivers as benchmarking;
- readiness and complete-stop polling across every declared endpoint;
- managed candidate crash/restart orchestration;
- required-property registration and enforcement;
- versioned result reporting with hidden replay-seed hashes; and
- reverse-order, retryable fixture cleanup journals.

`httpcheck` strictly validates HTTP responses and exact envelopes.
`jsoncheck` validates every collection row, exact object fields, field types,
and unique application-defined keys. These primitives default to rejection so
new accuracy adapters do not accidentally accept duplicate, partial, or
malformed collections.

Application endpoint mappings, seed oracles, entity relationships, and state
transitions do not belong here. They live in `accuracyapps/` and remain
independent of benchmark application validation.
