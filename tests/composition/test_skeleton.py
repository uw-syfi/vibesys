"""One scenario through the real core, shell and executors, with every gap named.

The run is built the way a launch will build it: real ``vs_core`` state and step,
the real vs-runtime shell with durable state under a temporary Project, the real
workspace executor on Git and the real evaluation executor on the Fake Slurm
cluster. It plays: baseline measured, one attempt (implementer turn, snapshot,
evaluate), settle, adopt, stop with the result. Two more scenarios repeat it with
a shell crash and restart, after a dispatch and after an observation.

Where a piece does not exist yet, the scenario fails at that interface and is
marked as a strict expected failure naming the missing piece and its owner. When
the owner lands, the marker turns into a failure ("unexpectedly passed") and the
merger removes it. The gap table is in the skeleton handoff.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest
from tests.support.executor_context import context_for
from tests.support.session_world import (
    ROLE,
    SCHEMA,
    Reply,
    dispatch_request,
    ensure_request,
)
from tests.support.skeleton_strategy import DECLARATION, DIGEST, SkeletonStrategy, measurement
from tests.support.skeleton_world import (
    CrashPoint,
    Process,
    World,
    drive,
    finished,
    open_skeleton_world,
    run_until_crash,
)

import vs_core
from vs_core.api import (
    CancelTurn,
    Capabilities,
    CloseSession,
    ContractError,
    DecisionId,
    InspectRequest,
    InvocationId,
    InvocationRef,
    LifecycleClass,
    ObservationStatus,
    OperationSchemaRef,
    RequestId,
    RevisionId,
    RevisionRef,
    RunFacts,
    RunStatus,
    SchemaRef,
    Scope,
    SessionId,
    SubmitMeasurement,
)
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    ExecutionResult,
    ExecutorRole,
    ObservationRejectedError,
    ReceiptCorruptError,
    RefusingRequestExecution,
    new_core_state,
    revision_ref,
)

LEASE = 100.0

# The first gap each scenario meets, in the order the run reaches it.
DIGEST_SCHEME = pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason=(
        "the baseline measurement is rejected: the workspace executor mints revision digest "
        "'git-commit:<sha>' (_workspace_requests.py:92) but the evaluation executor accepts only "
        "sha256 (_evaluation_jobs.py:87); gap A, owner EVAL-PATH (fix/eval-revision-path, not on "
        "main), probe test_a_workspace_revision_can_be_measured"
    ),
)
INSPECT_OF_SUBMIT = pytest.mark.xfail(
    raises=(AssertionError, ReceiptCorruptError),
    strict=True,
    reason=(
        "recovery of a dispatched request never resolves: core's InspectRequest is routed to "
        "the OPERATIONS executor (_core_requests.py:215), whose _target "
        "(_operation_requests.py:326) knows only operation receipts. For a SubmitMeasurement it "
        "answers UNKNOWN, so the intent stays reconciling; for a workspace request it reads the "
        "shared sealed entry as an operation ResultReceipt and raises ReceiptCorruptError "
        "(_operation_receipts.py:95), which halts the shell; owner OPS-OWNERS "
        "(_operation_requests.py), probes test_inspect_reports_a_recorded_measurement_submit and "
        "test_inspect_reports_a_recorded_workspace_request; core inspects every request that "
        "holds a resource after any restart, so no restart can complete until this is fixed"
    ),
)


class StepFailedError(AssertionError):
    """The run finished, but a step it needed was rejected or failed."""


def _start(world: World, host: str, now: float) -> Process:
    process = world.runtime()
    process.shell.start(host, now_at=now, lease_duration=LEASE)
    return process


def _assert_adopted(process: Process, world: World) -> None:
    strategy = process.shell.record.envelope.strategy
    if strategy.failure is not None:
        raise StepFailedError(strategy.failure)
    core = process.shell.record.envelope.core
    assert finished(process)
    assert core.run.status == RunStatus.TERMINAL
    assert core.run.result is not None
    assert core.run.result.outcome == "success"
    assert core.settlement.adoption is not None
    assert core.settlement.adoption.verified
    # Two measurements (baseline, candidate), each submitted to the cluster exactly once.
    assert len(world.cluster.submissions) == (2 if world.strategy.measured else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=DIGEST_SCHEME),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=INSPECT_OF_SUBMIT),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=DIGEST_SCHEME
        ),
    ],
)
async def test_skeleton(tmp_path: Path, crash: CrashPoint | None) -> None:
    await _play(tmp_path, crash, SkeletonStrategy())


# Gaps found behind SESSION_OUTCOME. Each was reproduced by a local, unpushed edit that
# fixed the gap in front of it (the patch is in the skeleton-2 handoff); with all of them
# applied the discarded-attempt scenario passes straight through. In the order met:
#   G2 owner SESSION-WIRING: a terminal DispatchTurn observation is never released
#      (_session_requests.py:301 _result), but attempt retirement drains a writer only from a
#      released invocation observation (_attempt_retirement.py:392 _invocation_drained), so a
#      closing attempt never reaches RetainRevision or DiscardWorkspace.
#   G3 owner CORE-P2: for CloseSession, the ledger (_intent_forward.py:209) and Sessions
#      (_session_turns.py:1025) both emit the same ReleaseDependencyObserved, and step raises
#      SignalCycleError (_step.py:634) on the duplicate.
#   G4 owner CORE-P2: _closure_event (_attempt_retirement.py:1680) makes progress only if the
#      released edge is still listed, but _discover has already dropped it once Sessions marked
#      the session TERMINAL, so the closing attempt stalls with nothing left to wait for.
#   G5 owner unassigned (CORE-P2 and the workspace executor): no request retains the commit a
#      plain write turn made. Core snapshots only on suspension or interrupt
#      (_session_turns.py:1316), and RetainRevision rejects a commit the run never minted
#      (_workspace_requests.py:376 _known_revision, :542), so a kept candidate cannot be
#      settled. The discarded-attempt scenario sidesteps it by adopting the trusted baseline.

SESSION_OUTCOME = pytest.mark.xfail(
    raises=ObservationRejectedError,
    strict=True,
    reason=(
        "core rejects the DispatchTurn observation: the executor puts the turn's output on "
        "RequestObserved (outcome_schema, outcome_json; _session_requests.py:329) and core's "
        "ingress requires a registered outcome proof for those fields "
        "(vs_core/_step.py:1202); the output belongs on the TurnObserved owner event only; "
        "owner SESSION-WIRING (_session_requests.py), probe "
        "test_a_turn_observation_passes_core_ingress"
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=SESSION_OUTCOME),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=INSPECT_OF_SUBMIT),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=INSPECT_OF_SUBMIT
        ),
    ],
)
async def test_skeleton_without_measurements(tmp_path: Path, crash: CrashPoint | None) -> None:
    """The attempt, turn, settle, adopt and stop path, with no evaluation in it."""
    await _play(tmp_path, crash, SkeletonStrategy.unmeasured())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=SESSION_OUTCOME),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=INSPECT_OF_SUBMIT),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=INSPECT_OF_SUBMIT
        ),
    ],
)
async def test_skeleton_discarded_attempt(tmp_path: Path, crash: CrashPoint | None) -> None:
    """One attempt is discarded and the trusted baseline adopted: no retained revision needed."""
    await _play(tmp_path, crash, SkeletonStrategy.unmeasured(keeps_candidate=False))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        None,
        pytest.param(CrashPoint.AFTER_DISPATCH, marks=INSPECT_OF_SUBMIT),
        pytest.param(CrashPoint.AFTER_OBSERVATION, marks=INSPECT_OF_SUBMIT),
    ],
)
async def test_skeleton_cancelled_attempt(tmp_path: Path, crash: CrashPoint | None) -> None:
    """Start one attempt and cancel the run: workspace acquired, scope closed, workspace discarded."""
    with open_skeleton_world(tmp_path, SkeletonStrategy.cancelled()) as world:
        process, now = await _started(world, crash)
        assert await drive(process, start=now) is None
        core = process.shell.record.envelope.core
        assert core.run.status == RunStatus.TERMINAL
        assert core.run.result is not None
        assert core.run.result.outcome == "cancelled"


async def _play(tmp_path: Path, crash: CrashPoint | None, strategy: SkeletonStrategy) -> None:
    with open_skeleton_world(tmp_path, strategy) as world:
        process, now = await _started(world, crash)
        assert await drive(process, start=now) is None
        _assert_adopted(process, world)


async def _started(world: World, crash: CrashPoint | None) -> tuple[Process, float]:
    """A shell started on a fresh run, or restarted after a crash at ``crash``, and its clock."""
    process = _start(world, "host-a", 0.0)
    now = 1.0
    if crash is not None:
        now = await run_until_crash(process, crash, start=now)
        process = _start(world, "host-b", now + LEASE + 1.0)
        now += LEASE + 2.0
    return process, now


# Interface probes. Each runs one real interface in isolation, so a gap stays visible
# while an earlier gap hides it from the full scenario above.


@pytest.mark.asyncio
@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason=(
        "a revision the workspace executor mints (digest 'git-commit:<sha>', "
        "_workspace_requests.py revision_ref) is rejected by the evaluation executor, which "
        "accepts only a sha256 content address (_evaluation_jobs.py _digest), so no workspace "
        "revision, baseline included, can be measured; gap A, owner EVAL-PATH "
        "(fix/eval-revision-path)"
    ),
)
async def test_a_workspace_revision_can_be_measured(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        commit = world.env.hosts[0].root.revision
        assert commit is not None
        request = SubmitMeasurement(
            request_id=RequestId(root="probe-submit"),
            scope=Scope(owner=world.initial().run.run_id, generation=0),
            admission_id=DecisionId(root="probe-admission"),
            deadline_at=100.0,
            plan=measurement(revision_ref(commit), "baseline"),
        )
        result = await executors.evaluation.execute(request, context_for(request))
        assert isinstance(result, ExecutionResult), result
        assert result.observation.observation.status != ObservationStatus.REJECTED, (
            result.observation.observation.diagnostic
        )


def test_every_request_role_has_a_production_executor(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        bound = {
            ExecutorRole.WORKSPACES: executors.workspaces,
            ExecutorRole.SESSIONS: executors.sessions,
            ExecutorRole.EVALUATION: executors.evaluation,
            ExecutorRole.OPERATIONS: executors.operations,
            ExecutorRole.SEMANTIC_EVENTS: executors.semantic_events,
        }
        unbound = sorted(
            role.value
            for role, role_executor in bound.items()
            if isinstance(role_executor, RefusingRequestExecution)
        )
        assert not unbound, f"roles with no production executor: {unbound}"


ADOPTION = frozenset({"AdoptRevision", "VerifyAdoption"})
RETIREMENT = frozenset({"CloseAttemptScope", "DiscardWorkspace", "RetainRevision"})


def _unproduced() -> set[str]:
    produced = _kinds_constructed_by_core()
    return {kind.__name__ for kind in REQUEST_DISPATCH if kind.__name__ not in produced}


def test_adoption_requests_have_a_core_producer() -> None:
    assert not _unproduced() & ADOPTION


def test_retirement_requests_have_a_core_producer() -> None:
    assert not _unproduced() & RETIREMENT


@pytest.mark.asyncio
@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason=(
        "nothing starts the observe cycle of a submitted measurement: the submit executor "
        "returns only MeasurementSubmissionObserved (_evaluation_requests.py _submit, owner_events) "
        "and core's _submission_job (vs_core/_measurements.py) issues no request, so the first "
        "ObserveOwnedJob is never emitted and the baseline stays pending (gap B). Also, a later "
        "JobObserved names the submission request but core's _source requires it to equal the "
        "submission intent's own observation (_measurements.py _source, row.observation != "
        "observation), so even once started it is dropped silently (gap C); both owned by EVAL-PATH "
        "(fix/eval-revision-path)"
    ),
)
async def test_a_submitted_measurement_starts_its_observe_cycle(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        commit = world.env.hosts[0].root.revision
        assert commit is not None
        # A sha256 address sidesteps the digest gap probed above.
        plan = measurement(
            RevisionRef(
                revision_id=RevisionId(root=commit),
                digest=hashlib.sha256(commit.encode()).hexdigest(),
            ),
            "baseline",
        )
        request = SubmitMeasurement(
            request_id=RequestId(root="probe-submit"),
            scope=Scope(owner=world.initial().run.run_id, generation=0),
            admission_id=DecisionId(root="probe-admission"),
            deadline_at=100.0,
            plan=plan,
        )
        result = await executors.evaluation.execute(request, context_for(request))
        assert isinstance(result, ExecutionResult), result
        kinds = [type(event).__name__ for event in result.owner_events]
        assert "JobObserved" in kinds, kinds


@pytest.mark.asyncio
async def test_session_lifecycle_requests_are_executed(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        scope = Scope(owner=world.initial().run.run_id, generation=0)
        session = SessionId(root="implementer")
        requests = (
            CloseSession(
                request_id=RequestId(root="probe-close"),
                scope=scope,
                deadline_at=100.0,
                session_id=session,
            ),
            CancelTurn(
                request_id=RequestId(root="probe-cancel"),
                scope=scope,
                deadline_at=100.0,
                invocation=InvocationRef(
                    session_id=session, invocation_id=InvocationId(root="inv"), generation=0
                ),
            ),
        )
        for request in requests:
            result = await executors.sessions.execute(request, context_for(request))
            assert isinstance(result, ExecutionResult), result
            diagnostic = result.observation.observation.diagnostic
            assert "not executed here" not in diagnostic, f"{request.kind}: {diagnostic}"


@pytest.mark.asyncio
@INSPECT_OF_SUBMIT
async def test_inspect_reports_a_recorded_measurement_submit(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        commit = world.env.hosts[0].root.revision
        assert commit is not None
        scope = Scope(owner=world.initial().run.run_id, generation=0)
        plan = measurement(
            RevisionRef(
                revision_id=RevisionId(root=commit),
                digest=hashlib.sha256(commit.encode()).hexdigest(),
            ),
            "baseline",
        )
        submit = SubmitMeasurement(
            request_id=RequestId(root="probe-submit"),
            scope=scope,
            admission_id=DecisionId(root="probe-admission"),
            deadline_at=100.0,
            plan=plan,
        )
        submitted = await executors.evaluation.execute(submit, context_for(submit))
        assert isinstance(submitted, ExecutionResult), submitted
        inspect = InspectRequest(
            request_id=RequestId(root="probe-inspect"),
            scope=scope,
            deadline_at=100.0,
            target=RequestId(root="probe-submit"),
        )
        inspected = await executors.operations.execute(inspect, context_for(inspect))
        assert isinstance(inspected, ExecutionResult), inspected
        target = inspected.observation.target
        assert target is not None
        assert target.observation.status != ObservationStatus.UNKNOWN, target.observation.diagnostic


@pytest.mark.asyncio
@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason=(
        "the DispatchTurn observation carries the turn's output (outcome_schema, outcome_json; "
        "_session_requests.py:329), which core's ingress rejects without a registered outcome "
        "proof (vs_core/_step.py:1202); the output belongs on TurnObserved only; owner "
        "SESSION-WIRING (_session_requests.py)"
    ),
)
async def test_a_turn_observation_passes_core_ingress(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        world.agents.resolver.roles = frozenset({ROLE})
        world.agents.resolver.schemas = {SCHEMA: Reply}
        executors = world.bindings().executors
        for request in (ensure_request(), dispatch_request()):
            result = await executors.sessions.execute(request, context_for(request))
            assert isinstance(result, ExecutionResult), result
        observed = result.observation
        assert observed.outcome_json is not None
        assert observed.outcome_is_registered, (
            "output on RequestObserved without a registered proof"
        )


def test_a_declaration_requiring_an_unoffered_operation_is_refused_by_name() -> None:
    declaration = DECLARATION.model_copy(
        update={
            "required_operations": (
                OperationSchemaRef(
                    kind="needs-this-operation",
                    request_schema=SchemaRef(name="request", version=1),
                    outcome_schema=SchemaRef(name="outcome", version=1),
                    lifecycle=LifecycleClass.QUERY,
                ),
            )
        }
    )
    facts = RunFacts(
        objective="x",
        baseline=revision_ref("0" * 40),
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
    )
    with pytest.raises(ContractError, match="required operation unavailable") as refused:
        new_core_state("run", facts, declaration, offered=Capabilities(), deadline_at=10.0)
    assert "needs-this-operation" in str(refused.value)


def test_every_unproduced_request_kind_has_a_named_owner() -> None:
    """A request kind that core never emits and no marker above covers is a new gap."""
    assert not _unproduced() - ADOPTION - RETIREMENT


def _kinds_constructed_by_core() -> set[str]:
    """Names of every class that vs-core source calls as a constructor."""
    names: set[str] = set()
    for source in Path(vs_core.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                # A class is produced when called, or passed to a helper that calls it.
                for part in (node.func, *node.args):
                    if isinstance(part, ast.Name):
                        names.add(part.id)
    return names
