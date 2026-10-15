"""A revision an agent named is untrusted: the run ends it in a typed outcome, never in a hang.

A search often reads the revision to measure or keep from an agent's reply. The reply is
schema-valid whatever it names, so the executors that act on the name check it against the
repository the run holds. These tests drive the production workspace and evaluation executors
over in-memory Git and the Fake Slurm cluster, with revisions that exist, do not exist, are
malformed or belong to another workspace.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.skeleton_world import World, open_skeleton_world

from vs_core.api import (
    AttemptId,
    AttemptRef,
    CancelOwnedJob,
    CloseAttemptScope,
    CollectEvidence,
    ContractValidationError,
    InspectOwnedJob,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    RevisionRef,
    SubmitMeasurement,
)
from vs_project.api import GitCommandError, Project
from vs_runtime.api import WorkspaceRestoreError
from vs_runtime.api.core import ExecutionResult, MeasurementRequests, ReceiptStore
from vs_runtime.api.executor_contexts import context_for
from vs_runtime.api.observation_contracts import assert_core_accepts
from vs_runtime.api.slurm_evaluation_stack import (
    ADMISSION,
    SCOPE,
    ScenarioCluster,
    build_stack,
    plan,
    submission,
)
from vs_sim.api.testing import SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import RequestBase
    from vs_evaluation.api import (
        AvailabilitySnapshot,
        EvaluationRequest,
        ExecutorObservation,
        ExecutorPoll,
        PollingEvaluationExecutor,
        ResourceRequirements,
    )


# Each example builds a run. A pull request draws a sample; the nightly depth searches deeper.
_EXAMPLES = settings(
    max_examples=examples(pr=5, nightly=40),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _hex40() -> st.SearchStrategy[str]:
    return st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)


def _named_revision(held: tuple[str, ...]) -> st.SearchStrategy[str]:
    """What an agent may write where a commit goes: held commits, absent ones, junk."""
    junk = st.one_of(
        st.text(min_size=1, max_size=24),
        st.sampled_from(["HEAD", "main", "HEAD~1", "-x", "--output=/tmp/x", "a\x00b", "../x"]),
        st.text(alphabet="0123456789abcdefABCDEF", min_size=1, max_size=64),
    )
    return st.one_of(st.sampled_from(held), _hex40(), junk)


def _ref(revision: str) -> RevisionRef | None:
    """The reference a search mints from a named commit, or None when minting refuses it."""
    try:
        return RevisionRef.of_git_commit(revision)
    except (ContractValidationError, ValidationError):
        return None


async def _commits_of(world: World) -> tuple[str, ...]:
    """The commits the run's repository holds: its baseline and one another workspace made."""
    root = world.env.hosts[0].root
    baseline = root.trusted_input_baseline
    assert baseline is not None
    other = await world.env.hosts[0].create_candidate(baseline)
    (other.path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    foreign = await other.snapshot("another-workspace")
    assert foreign != baseline
    return baseline, foreign


# -- the workspace owner ---------------------------------------------------------------------


@given(data=st.data())
@_EXAMPLES
async def test_a_candidate_is_opened_from_a_held_revision_and_refused_from_any_other(
    data: st.DataObject, tmp_path: Path
) -> None:
    """The refusal is the contract's typed error, never the Git failure of the worktree add."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as raw, open_skeleton_world(Path(raw)) as world:
        held = await _commits_of(world)
        revision = data.draw(_named_revision(held))
        workspaces = world.env.hosts[0]

        try:
            candidate = await workspaces.create_candidate(revision)
        except WorkspaceRestoreError:
            assert revision not in held
        except GitCommandError as error:  # pragma: no cover - the defect this test pins
            pytest.fail(f"Git failed inside the worktree add for {revision!r}: {error}")
        else:
            assert revision in held
            await candidate.discard()


# -- the evaluation owner --------------------------------------------------------------------


def _close(name: str = "close") -> CloseAttemptScope:
    return CloseAttemptScope(
        request_id=RequestId(root=name),
        scope=SCOPE,
        admission_id=ADMISSION,
        attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
    )


@given(data=st.data())
@_EXAMPLES
async def test_a_measurement_of_a_named_revision_is_accepted_or_refused_and_the_scope_still_closes(
    data: st.DataObject, tmp_path: Path
) -> None:
    """A revision the run holds is measured; any other is a typed refusal that launches nothing,
    lists no job at the close, and leaves the close able to release the scope."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as raw, open_skeleton_world(Path(raw)) as world:
        held = await _commits_of(world)
        named = data.draw(_named_revision(held))
        ref = _ref(named)
        if ref is None:
            return
        evaluation = cast("MeasurementRequests", world.bindings().executors.evaluation)
        request = SubmitMeasurement(
            request_id=RequestId(root="sub"),
            scope=SCOPE,
            admission_id=ADMISSION,
            plan=plan().model_copy(update={"candidate": ref}),
        )

        submitted = await evaluation.execute(request, context_for(request))
        observed = submitted.observation.observation
        if named in held:
            assert observed.accepted
            return
        assert observed.status is ObservationStatus.REJECTED
        assert observed.terminal
        assert world.cluster.submissions == []

        closing = _close()
        closed = await evaluation.execute(closing, context_for(closing))
        seen = closed.observation.observation
        assert seen.released, seen.diagnostic
        assert seen.children == ()
        assert seen.children_complete
        assert_core_accepts([submitted, closed], expect_retry=False)


# -- an executor that fails in an unexpected way ---------------------------------------------


class _Failing:
    """The world's executor, except that the named calls raise ``error``.

    ``submit`` never reaches the real executor when it raises, so the job never exists. The
    rest is the real executor.
    """

    def __init__(
        self, inner: PollingEvaluationExecutor, error: Callable[[], Exception], failing: set[str]
    ) -> None:
        self._inner = inner
        self._error = error
        self._failing = failing

    def _maybe_raise(self, call: str) -> None:
        if call in self._failing:
            raise self._error()

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        self._maybe_raise("availability")
        return await self._inner.availability(requirements)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        self._maybe_raise("submit")
        await self._inner.submit(request, handle_id=handle_id)

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        self._maybe_raise("inspect_only")
        return await self._inner.inspect_only(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        self._maybe_raise("inspect")
        return await self._inner.inspect(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        self._maybe_raise("wait_for_change")
        await self._inner.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        self._maybe_raise("cancel")
        await self._inner.cancel(handle_id)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        self._maybe_raise("poll")
        return await self._inner.poll(handle_id)


_ERRORS: tuple[Callable[[], Exception], ...] = (
    lambda: RuntimeError("the executor broke"),
    lambda: OSError(28, "no space left on device"),
    lambda: KeyError("handle"),
    lambda: ValueError("bad"),
    lambda: TimeoutError("slow"),
    lambda: AttributeError("none"),
    lambda: GitCommandError(["git", "worktree", "add"], 128, "fatal: invalid reference: x"),
    lambda: WorkspaceRestoreError("x"),
)
_CALLS = (
    "availability",
    "submit",
    "inspect_only",
    "inspect",
    "wait_for_change",
    "cancel",
    "poll",
)
_KINDS = (
    SubmitMeasurement,
    ObserveOwnedJob,
    InspectOwnedJob,
    CollectEvidence,
    CancelOwnedJob,
    CloseAttemptScope,
)


def _requests_over(base: Path, jobs: PollingEvaluationExecutor) -> MeasurementRequests:
    project = Project.open(base / "project")
    return MeasurementRequests(
        jobs, ReceiptStore(project.state.state_store_namespace("run"), threads=SimThreads())
    )


def _request(
    kind: type[RequestBase], resource: ResourceId, name: str, candidate: str
) -> RequestBase:
    if kind is SubmitMeasurement:
        return submission(name, candidate=candidate)
    if kind is CloseAttemptScope:
        return _close(name)
    return cast("Callable[..., RequestBase]", kind)(
        request_id=RequestId(root=name), scope=SCOPE, admission_id=ADMISSION, resource_id=resource
    )


@given(
    error=st.sampled_from(_ERRORS),
    failing=st.sets(st.sampled_from(_CALLS), min_size=1),
    kind=st.sampled_from(_KINDS),
)
@_EXAMPLES
async def test_an_executor_that_raises_leaves_every_request_kind_with_a_typed_outcome(
    error: Callable[[], Exception],
    failing: set[str],
    kind: type[RequestBase],
    tmp_path: Path,
) -> None:
    """Whatever the executor raises, at whichever call, the request ends in an observation."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as raw:
        base = Path(raw)
        (base / "project").mkdir()
        stack = await build_stack(base / "stack", ScenarioCluster())
        try:
            sub = submission("seed", candidate=stack.snapshot)
            healthy = _requests_over(base, stack.executor)
            seeded = await healthy.execute(sub, context_for(sub))
            resource = seeded.observation.observation.resource_id
            assert resource is not None

            faulty = _requests_over(base, _Failing(stack.executor, error, failing))
            request = _request(kind, resource, "probe", stack.snapshot)
            result = await faulty.execute(cast("SubmitMeasurement", request), context_for(request))

            assert isinstance(result, ExecutionResult)
            assert result.observation.observation.request_id == request.request_id
            assert_core_accepts([result], expect_retry=False)
        finally:
            await stack.executor.close()


@given(
    error=st.sampled_from(_ERRORS),
    failing=st.sets(st.sampled_from(("submit", "cancel")), min_size=1),
)
@_EXAMPLES
async def test_a_scope_closes_over_a_job_whose_launch_failed_however_its_cancel_fails(
    error: Callable[[], Exception], failing: set[str], tmp_path: Path
) -> None:
    """A job that never launched has nothing to cancel: the close reads the executor, not the
    failed cancel, so it releases instead of reporting a job it can never end."""
    with tempfile.TemporaryDirectory(dir=tmp_path) as raw:
        base = Path(raw)
        (base / "project").mkdir()
        stack = await build_stack(base / "stack", ScenarioCluster())
        try:
            wedged = _requests_over(base, _Failing(stack.executor, error, failing | {"submit"}))
            sub = submission("sub", candidate=stack.snapshot)
            submitted = await wedged.execute(sub, context_for(sub))

            closing = _close()
            closed = await wedged.execute(closing, context_for(closing))

            seen = closed.observation.observation
            assert seen.released, seen.diagnostic
            assert stack.cluster.submissions == []
            assert_core_accepts([submitted, closed], expect_retry=False)
        finally:
            await stack.executor.close()
