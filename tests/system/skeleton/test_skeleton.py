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
import functools
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest
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
from tests.support.skeleton_strategy import (
    DECLARATION,
    DIGEST,
    SkeletonConfig,
    SkeletonStrategy,
    measure,
    measurement,
)
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
    DecisionId,
    DispatchTurn,
    EnsureSession,
    InspectRequest,
    InspectTurn,
    InvocationId,
    InvocationRef,
    LifecycleClass,
    MeasurementResult,
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
from vs_core.api.testing import sample_limits
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    CoreStartup,
    ExecutionResult,
    ExecutorRole,
    RefusingRequestExecution,
    empty_catalog,
    new_core_state,
    revision_ref,
)
from vs_runtime.api.executor_contexts import context_for
from vs_sim.api.testing import Depth, depth

LEASE = 100.0


def _crashes(pull_request: tuple[CrashPoint | None, ...] = (None,)) -> list[object]:
    """The restarts a scenario is played with: a pull request tries ``pull_request``.

    The crash sweeps of ``tests/system/crash`` restart this very search at every kind of
    boundary, so a pull request plays the other scenarios straight through (the cancelled one
    also with one restart, which the sweeps do not reach); the nightly job restarts each after
    the dispatch and after the observation as well.
    """
    chosen = (
        (None, CrashPoint.AFTER_DISPATCH, CrashPoint.AFTER_OBSERVATION)
        if depth() is Depth.NIGHTLY
        else pull_request
    )
    return [
        pytest.param(crash, id="straight-through" if crash is None else f"crash-{crash.name}")
        for crash in chosen
    ]


def _start(world: World, host: str, now: float) -> Process:
    process = world.runtime()
    process.shell.start(host, now_at=now, lease_duration=LEASE)
    return process


def _assert_adopted(process: Process, world: World) -> None:
    core = process.shell.record.envelope.core
    assert finished(process)
    assert core.run.status == RunStatus.TERMINAL
    assert core.run.result is not None
    assert core.run.result.outcome == "success"
    assert core.settlement.adoption is not None
    assert core.settlement.adoption.verified
    # Two measurements (baseline, candidate), each submitted to the cluster exactly once.
    assert len(world.cluster.submissions) == (2 if world.strategy.measured else 0)


@pytest.mark.parametrize("crash", _crashes())
async def test_skeleton(tmp_path: Path, crash: CrashPoint | None) -> None:
    await _play(tmp_path, crash, SkeletonStrategy())


@pytest.mark.parametrize("crash", _crashes())
async def test_skeleton_without_measurements(tmp_path: Path, crash: CrashPoint | None) -> None:
    """The attempt, turn, settle, adopt and stop path, with no evaluation in it."""
    await _play(tmp_path, crash, SkeletonStrategy.unmeasured())


@pytest.mark.parametrize("crash", _crashes())
async def test_skeleton_discarded_attempt(tmp_path: Path, crash: CrashPoint | None) -> None:
    """One attempt is discarded and the trusted baseline adopted: no retained revision needed."""
    await _play(tmp_path, crash, SkeletonStrategy.unmeasured(keeps_candidate=False))


@pytest.mark.parametrize("crash", _crashes(pull_request=(CrashPoint.AFTER_DISPATCH,)))
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


async def test_a_workspace_revision_can_be_measured(tmp_path: Path) -> None:
    with open_skeleton_world(tmp_path) as world:
        executors = world.bindings().executors
        commit = world.env.hosts[0].root.revision
        assert commit is not None
        request = SubmitMeasurement(
            request_id=RequestId(root="probe-submit"),
            scope=Scope(owner=world.initial().run.run_id, generation=0),
            admission_id=DecisionId(root="probe-admission"),
            plan=measurement(revision_ref(commit), "baseline"),
        )
        result = await executors.evaluation.execute(request, context_for(request))
        assert isinstance(result, ExecutionResult), result
        assert result.observation.observation.status != ObservationStatus.REJECTED, (
            result.observation.observation.diagnostic
        )


async def test_every_request_role_has_a_production_executor(tmp_path: Path) -> None:
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
            invocation=invocation,
        ),
        CloseSession(
            request_id=RequestId(root="probe-close"),
            scope=SCOPE,
            session_id=SESSION,
        ),
        ResumeSessionTurn(
            request_id=RequestId(root="probe-resume"),
            scope=SCOPE,
            turn=turn_spec(),
            continuation_id=ContinuationId(root="probe-continuation"),
        ),
    )


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
            plan=plan,
        )
        submitted = await executors.evaluation.execute(submit, context_for(submit))
        assert isinstance(submitted, ExecutionResult), submitted
        inspect = InspectRequest(
            request_id=RequestId(root="probe-inspect"),
            scope=scope,
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
        new_core_state(
            "run",
            facts,
            declaration,
            offered=empty_catalog(),
            startup=CoreStartup(deadline_at=10.0, limits=sample_limits()),
        )
    assert "needs-this-operation" in str(refused.value)


@pytest.mark.parametrize(
    "status", [ObservationStatus.REJECTED, ObservationStatus.CANCELLED, ObservationStatus.FAILED]
)
async def test_a_measurement_that_is_not_evidence_is_the_reason_the_run_has_no_winner(
    status: ObservationStatus,
) -> None:
    """A rejected or cancelled result may carry no typed failure; it still ends the run."""
    result = MeasurementResult.model_construct(status=status, failure=None)

    async def measure_it(*_args: object, **_kwargs: object) -> MeasurementResult:
        return result

    facts = SimpleNamespace(
        evaluator_digest=DIGEST,
        workload_digest=DIGEST,
        environment_digest=DIGEST,
    )
    ctx = SimpleNamespace(evaluation=SimpleNamespace(measure=measure_it), facts=facts)
    baseline = RevisionRef.of_git_commit("a" * 40)

    refused = await measure(ctx, SkeletonConfig(), baseline, "baseline")  # ty: ignore[invalid-argument-type]

    assert refused is not None
    assert status.value in refused.reason


def test_every_unproduced_request_kind_has_a_named_owner() -> None:
    """A request kind that core never emits and no marker above covers is a new gap."""
    assert not _unproduced() - ADOPTION - RETIREMENT


@functools.cache
def _kinds_constructed_by_core() -> frozenset[str]:
    """Names of every class that vs-core source calls as a constructor.

    Parsing vs-core's source costs seconds, and the answer is fixed for the process.
    """
    names: set[str] = set()
    for source in Path(vs_core.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                # A class is produced when called, or passed to a helper that calls it.
                for part in (node.func, *node.args):
                    if isinstance(part, ast.Name):
                        names.add(part.id)
    return frozenset(names)
