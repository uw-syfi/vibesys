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
    SCOPE,
    SESSION,
    Reply,
    dispatch_request,
    ensure_request,
    inspect_request,
    turn_spec,
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
    CloseSession,
    ContinuationId,
    ContractError,
    ContractValidationError,
    DecisionId,
    DispatchTurn,
    EnsureSession,
    InspectRequest,
    InspectTurn,
    InvocationId,
    InvocationRef,
    LifecycleClass,
    ObservationStatus,
    OperationSchemaRef,
    RequestId,
    ResumeSessionTurn,
    RevisionId,
    RevisionRef,
    RunFacts,
    RunStatus,
    SchemaRef,
    Scope,
    SubmitMeasurement,
)
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    ExecutionResult,
    ExecutorRole,
    RefusingRequestExecution,
    empty_catalog,
    new_core_state,
    revision_ref,
)

LEASE = 100.0


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


TURN_OUTPUT_DROPPED = pytest.mark.xfail(
    raises=(AssertionError, ContractValidationError),
    strict=True,
    reason=(
        "a write turn's structured reply never reaches the strategy: the turn output cannot ride "
        "RequestObserved (core's ingress needs a registered outcome proof, vs_core/_step.py), so "
        "it travels on the executor's TurnObserved owner event; but core derives its own "
        "TurnObserved from the intent ledger (vs_core/_intent_forward.py, DispatchTurn case) with "
        "output_json=None, applies it first, and drops the executor's event as a replay of the "
        "same sequence (vs_core/_session_turns.py _turn_observed). TurnResult.output_json is "
        "None, so the candidate commit is unknown and the retained settlement is refused. "
        "Owner CORE-P2 (core derives the output-less event) with SESSION-WIRING "
        "(_session_requests.py owes the output on the observation the ledger sees)"
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=TURN_OUTPUT_DROPPED),
        pytest.param(
            CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=TURN_OUTPUT_DROPPED
        ),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=TURN_OUTPUT_DROPPED
        ),
    ],
)
async def test_skeleton(tmp_path: Path, crash: CrashPoint | None) -> None:
    await _play(tmp_path, crash, SkeletonStrategy())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=TURN_OUTPUT_DROPPED),
        pytest.param(
            CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=TURN_OUTPUT_DROPPED
        ),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=TURN_OUTPUT_DROPPED
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
        pytest.param(None, id="straight-through"),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch"),
        pytest.param(CrashPoint.AFTER_OBSERVATION, id="crash-after-observation"),
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
        CrashPoint.AFTER_DISPATCH,
        CrashPoint.AFTER_OBSERVATION,
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


type SessionRequest = (
    EnsureSession | DispatchTurn | InspectTurn | CancelTurn | CloseSession | ResumeSessionTurn
)


def _session_requests() -> tuple[SessionRequest, ...]:
    """One request of each of the six kinds the session role serves."""
    invocation = InvocationRef(
        session_id=SESSION, invocation_id=InvocationId(root="inv-1"), generation=0
    )
    return (
        ensure_request(),
        dispatch_request(),
        inspect_request(),
        CancelTurn(
            request_id=RequestId(root="probe-cancel"),
            scope=SCOPE,
            deadline_at=100.0,
            invocation=invocation,
        ),
        CloseSession(
            request_id=RequestId(root="probe-close"),
            scope=SCOPE,
            deadline_at=100.0,
            session_id=SESSION,
        ),
        ResumeSessionTurn(
            request_id=RequestId(root="probe-resume"),
            scope=SCOPE,
            deadline_at=100.0,
            turn=turn_spec(),
            continuation_id=ContinuationId(root="probe-continuation"),
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("request_", _session_requests(), ids=lambda request: request.kind)
async def test_every_session_request_reaches_an_executor_that_serves_it(
    tmp_path: Path, request_: SessionRequest
) -> None:
    """The session role built by ``core_bindings`` serves all six kinds: turns take
    EnsureSession, DispatchTurn and InspectTurn, lifecycle takes the other three, and neither
    answers a kind it does not own with 'not executed here'.
    """
    with open_skeleton_world(tmp_path) as world:
        world.agents.resolver.roles = frozenset({ROLE})
        world.agents.resolver.schemas = {SCHEMA: Reply}
        executors = world.bindings().executors
        result = await executors.sessions.execute(request_, context_for(request_))
        assert isinstance(result, ExecutionResult), result
        diagnostic = result.observation.observation.diagnostic
        assert "not executed here" not in diagnostic, f"{request_.kind}: {diagnostic}"


@pytest.mark.asyncio
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
        new_core_state("run", facts, declaration, offered=empty_catalog(), deadline_at=10.0)
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
