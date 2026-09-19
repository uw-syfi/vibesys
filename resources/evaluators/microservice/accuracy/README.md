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

`Program`, `Reference`, and `VerifyProgram` provide a common model for
application-owned sequential, concurrent, and crash-recovery checks. A program
is replayable data containing calls, parallel call groups, and quiescent
lifecycle events. The reference model defines one canonical sequential state
transition; the verifier uses it directly for sequential calls and explores
legal serializations for parallel groups. See [Event programs and reference
oracles](PROGRAMS.md) for the complete contract, execution semantics,
limitations, and an application-neutral example.

`Case` and `VerifyCase` remain as a sequential compatibility API. The shared
verifiers do not remove actions automatically, because doing so is unsafe for
stateful histories without an application-specific dependency and reset
contract.

Application endpoint mappings, seed oracles, entity relationships, generated
input grammars, and state transitions do not belong here. They live with the
task or example and remain independent of benchmark application validation.
`accuracyapps/` contains legacy bundled adapters for existing workloads; it is
not required for task-owned extensions.

The runner rejects readiness declarations that omit or invent workload targets
and transport-gates every semantic readiness validator. Registry composition
also requires a factory's application identity to match its workload key.
