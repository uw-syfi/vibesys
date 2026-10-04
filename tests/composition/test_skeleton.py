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
from tests.support.skeleton_strategy import measurement
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
    DecisionId,
    ObservationStatus,
    RequestId,
    RevisionId,
    RevisionRef,
    RunStatus,
    Scope,
    SubmitMeasurement,
)
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    CoreContractGapError,
    ExecutorRole,
    RefusingRequestExecution,
    revision_ref,
)

LEASE = 100.0

# The first gap every scenario meets, in the order the run reaches it.
INTENT_LEDGER = pytest.mark.xfail(
    raises=CoreContractGapError,
    strict=True,
    reason=(
        "vs-core intent ledger is a stub (_intent_ledger.py: dispatch_authorized raises "
        "KernelNotImplementedError); owner #1319 feat/core-intent-ledger"
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
    assert len(world.cluster.submissions) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "crash",
    [
        pytest.param(None, id="straight-through", marks=INTENT_LEDGER),
        pytest.param(CrashPoint.AFTER_DISPATCH, id="crash-after-dispatch", marks=INTENT_LEDGER),
        pytest.param(
            CrashPoint.AFTER_OBSERVATION, id="crash-after-observation", marks=INTENT_LEDGER
        ),
    ],
)
async def test_skeleton(tmp_path: Path, crash: CrashPoint | None) -> None:
    with open_skeleton_world(tmp_path) as world:
        process = _start(world, "host-a", 0.0)
        now = 1.0
        if crash is not None:
            now = await run_until_crash(process, crash, start=now)
            process = _start(world, "host-b", now + LEASE + 1.0)
            now += LEASE + 2.0
        assert await drive(process, start=now) is None
        _assert_adopted(process, world)


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
        "revision, baseline included, can be measured; no owner (contracts-a added DigestScheme "
        "but neither side consumes it)"
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
        assert result.observation.observation.status != ObservationStatus.REJECTED, (
            result.observation.observation.diagnostic
        )


@pytest.mark.xfail(
    raises=AssertionError,
    strict=True,
    reason=(
        "no production SessionRequests exists (EnsureSession, DispatchTurn, InspectTurn, "
        "CancelTurn, CloseSession, ResumeSessionTurn); owner lane B (design.md section 5, "
        "runtime _core_sessions), no open PR; wip/feat/core-sessions-b has no runtime executor"
    ),
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


@pytest.mark.xfail(
    strict=True,
    reason=(
        "vs-core attempt retirement is a stub (_attempt_retirement.py); "
        "owner #1289 feat/core-attempt-retirement"
    ),
)
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
        "ObserveOwnedJob is never emitted and the baseline stays pending; no owner. Also, a later "
        "JobObserved names the submission request but core's _source requires it to equal the "
        "submission intent's own observation (_measurements.py _source, row.observation != "
        "observation), so even once started it is dropped silently"
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
        kinds = [type(event).__name__ for event in result.owner_events]
        assert "JobObserved" in kinds, kinds


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
