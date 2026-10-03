# vs-evaluation

## Responsibility

This library owns reusable evaluation infrastructure. It coordinates durable,
provider-neutral requests and exposes a role-limited agent service over a
private socket. It defines semantic evidence contracts, evaluation and profiler
operation protocols, authorization, bounded waiting, cancellation, availability,
and recovery after process restart. Product code supplies trust policy,
candidate snapshots, evaluator implementations, and profiler turn provision.

## Public API

Import lifecycle and agent service contracts from vs_evaluation.api. Import MCP
tool construction from vs_evaluation.api.tools. Callers provide ordered named
stages and a stable key:

    from vs_evaluation.api import (
        EvaluationCoordinator,
        EvaluationRequest,
        EvaluationStep,
        FilesystemEvaluationStore,
    )

    request = EvaluationRequest(
        key="candidate-revision-and-contract-digest",
        stages=(
            EvaluationStep(name="correctness", payload={"command": ["pytest"]}),
            EvaluationStep(name="measurement", payload={"command": ["bench"]}),
        ),
    )

EvaluationCoordinator.submit returns an EvaluationHandle after durable claim
and idempotent executor submission. The handle can report status, request
cancellation, or await_result(timeout_s) with a required finite positive
timeout bounded by coordinator configuration. Await returns a discriminated
completed, timed-out, failed, or canceled result. Completed results preserve
each named stage's terminal state, JSON result, failure, and elapsed duration.
The deadline covers storage, executor inspection, and change waits. A timed-out
result carries the last observed durable status, or `null` if even the first
storage read could not finish before the deadline. Timeout never cancels work.

The executor must accept the same handle ID idempotently and support inspection
by that ID. This makes ambiguous submit errors safe to retry and lets
reconcile() restore accepted and active records after restart. The filesystem
store uses JSON records, atomic replacement, and an interprocess lock. It stores
no credentials.
Executor change notifications must be sticky across the inspect-to-wait gap.

## Effects and tests

EvaluationExecutor, EvaluationStore, and Clock are injected protocols.
FilesystemEvaluationStore is the production store. The public test doubles
are in vs_evaluation.api.testing and include a manual clock, controllable
executor, shared remote backend, in-memory store, and controllable profiler turn
provision. The executor can inject
ambiguous acceptance, observation timeouts, stale observations, partial wait
timeouts, and wait-time transitions without sleeping. Share one
`FakeEvaluationBackend` across fresh executor clients to exercise restart and
idempotent recovery against provider state that outlives the client.
