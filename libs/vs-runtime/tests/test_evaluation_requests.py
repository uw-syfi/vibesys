"""The EVALUATION role over the production executor stack on a Fake Slurm cluster."""

from __future__ import annotations

import hashlib
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.observation_contract import assert_core_accepts
from tests.support.runtime_evaluation import (
    ADMISSION,
    SCOPE,
    ScenarioCluster,
    Stack,
    build_stack,
    plan,
    submission,
)

from vs_core.api import (
    AttemptId,
    AttemptRef,
    CancelOwnedJob,
    CloseAttemptScope,
    CollectEvidence,
    DecisionId,
    HostFence,
    HostId,
    InspectOwnedJob,
    JobObserved,
    ObservationStatus,
    ObserveOwnedJob,
    RequestId,
    ResourceId,
    RevisionId,
    RevisionRef,
    SubmitMeasurement,
)
from vs_core.api.proofs import Proven, fresh_observation
from vs_project.api import Project
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    ExecutionContext,
    ExecutionResult,
    ExecutorRole,
    MeasurementRequests,
    ReceiptStore,
    RequestExecutors,
    revision_ref,
)
from vs_slurm.api import SlurmJobStatus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from pydantic import BaseModel

    from vs_core.api import Observation
    from vs_project.api import StateNamespace

pytestmark = pytest.mark.asyncio

type EvaluationRoleRequest = (
    SubmitMeasurement
    | ObserveOwnedJob
    | InspectOwnedJob
    | CollectEvidence
    | CancelOwnedJob
    | CloseAttemptScope
)


class CrashError(Exception):
    """Raised by CrashingStore to stand for the process dying at that write."""


class CrashingStore(ReceiptStore):
    """A receipt store that dies just before its ``crash_at``-th write."""

    def __init__(self, namespace: StateNamespace, crash_at: int | None = None) -> None:
        super().__init__(namespace)
        self.writes = 0
        self._crash_at = crash_at

    def _tick(self) -> None:
        if self._crash_at == self.writes:
            raise CrashError
        self.writes += 1

    def record_once(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        self._tick()
        super().record_once(family, part, key, receipt)

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        self._tick()
        super().replace(family, part, key, receipt)


class FakeLease:
    """A lease the test can revoke."""

    def __init__(self) -> None:
        self.valid = True

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        del now_at
        return self.valid


def context_for(
    request: EvaluationRoleRequest,
    lease: FakeLease | None = None,
    epoch: int = 1,
    now_at: float = 5.0,
) -> ExecutionContext:
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=epoch),
        now_at=now_at,
        payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        lease=lease or FakeLease(),
    )


class World:
    """One run's durable state, cluster and executors, reopenable after a simulated crash."""

    def __init__(self, base: Path, cluster: ScenarioCluster, stack: Stack) -> None:
        self.base = base
        self.cluster = cluster
        self.stack = stack

    def requests(self, crash_at: int | None = None) -> tuple[MeasurementRequests, CrashingStore]:
        project = Project.open(self.base / "project")
        store = CrashingStore(project.state.state_store_namespace("run"), crash_at)
        return MeasurementRequests(self.stack.executor, store), store


@asynccontextmanager
async def world(cluster: ScenarioCluster | None = None) -> AsyncIterator[World]:
    cluster = cluster or ScenarioCluster()
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "project").mkdir()
        stack = await build_stack(base / "stack", cluster)
        try:
            yield World(base, cluster, stack)
        finally:
            await stack.executor.close()


async def settled(w: World, resource: ResourceId) -> None:
    """Wait on the executor's lifecycle notification until the job reached the cluster."""
    while resource.root not in w.cluster.submissions:
        await w.stack.executor.wait_for_change(resource.root, 30.0)


def query[RequestT: ObserveOwnedJob | InspectOwnedJob | CollectEvidence | CancelOwnedJob](
    kind: type[RequestT], name: str, resource: ResourceId
) -> RequestT:
    return kind(
        request_id=RequestId(root=name),
        scope=SCOPE,
        admission_id=ADMISSION,
        deadline_at=100.0,
        resource_id=resource,
    )


def close_request(name: str = "close", admission: DecisionId = ADMISSION) -> CloseAttemptScope:
    return CloseAttemptScope(
        request_id=RequestId(root=name),
        scope=SCOPE,
        admission_id=admission,
        deadline_at=100.0,
        attempt=AttemptRef(attempt_id=AttemptId(root="attempt"), generation=0),
    )


async def submit(w: World, name: str = "sub") -> ExecutionResult:
    requests, _ = w.requests()
    sub = submission(name, candidate=w.stack.snapshot)
    return await requests.execute(sub, context_for(sub))


def resource_of(result: ExecutionResult) -> ResourceId:
    resource = result.observation.observation.resource_id
    assert resource is not None
    return resource


async def run(
    w: World, request: EvaluationRoleRequest, crash_at: int | None = None
) -> ExecutionResult:
    requests, _ = w.requests(crash_at)
    return await requests.execute(request, context_for(request))


# happy path


async def test_submit_observe_inspect_collect_round_trip() -> None:
    async with world() as w:
        first = await submit(w)
        obs = first.observation.observation
        assert obs.status is ObservationStatus.PENDING
        assert obs.accepted
        resource = resource_of(first)
        await settled(w, resource)
        assert w.cluster.submissions == [resource.root]
        inspected = await run(w, query(InspectOwnedJob, "inspect", resource))
        target = inspected.observation.target
        assert target is not None
        assert target.observation.status is ObservationStatus.SUCCEEDED
        assert target.observation.terminal
        assert target.evidence
        assert target.evaluation_result is not None
        assert target.evaluation_result.accuracy_passed
        collected = await run(w, query(CollectEvidence, "collect", resource))
        assert collected.observation.observation.terminal
        assert collected.observation.observation.status is ObservationStatus.SUCCEEDED
        assert w.cluster.submissions == [resource.root]


async def test_failed_benchmark_keeps_partial_stage_evidence() -> None:
    cluster = ScenarioCluster()
    cluster.benchmark_exit = 1
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        target = (await run(w, query(InspectOwnedJob, "inspect", resource))).observation.target
        assert target is not None
        assert target.observation.terminal
        assert target.observation.status is ObservationStatus.SUCCEEDED
        assert {ref.status for ref in target.evidence} == {
            ObservationStatus.SUCCEEDED,
            ObservationStatus.FAILED,
        }
        facts = target.evaluation_result
        assert facts is not None
        assert facts.accuracy_passed
        outcomes = {row.stage_id: row.outcome.value for row in facts.stages}
        assert outcomes == {"accuracy": "passed", "benchmark": "failed"}


async def test_observe_never_submits_or_creates_workspaces() -> None:
    async with world() as w:
        ghost = ResourceId(root="vs-" + "0" * 40)
        got = await run(w, query(ObserveOwnedJob, "observe-ghost", ghost))
        assert got.observation.observation.status is ObservationStatus.REJECTED
        assert w.cluster.submissions == []


# replay and sequences


async def test_replay_returns_the_stored_result_unchanged() -> None:
    async with world() as w:
        sub = submission(candidate=w.stack.snapshot)
        ctx = context_for(sub)
        requests, _ = w.requests()
        first = await requests.execute(sub, ctx)
        resource = resource_of(first)
        await settled(w, resource)
        assert await requests.execute(sub, ctx) == first
        inspect = query(InspectOwnedJob, "inspect", resource)
        one = await requests.execute(inspect, context_for(inspect))
        assert await requests.execute(inspect, context_for(inspect)) == one
        assert w.cluster.submissions == [resource.root]


async def test_same_request_with_another_payload_conflicts() -> None:
    async with world() as w:
        await submit(w)
        other = submission(override=plan(stages=("accuracy",), candidate=w.stack.snapshot))
        requests, _ = w.requests()
        outcome = await requests.execute(other, context_for(other))
        assert outcome.observation.observation.status is ObservationStatus.REJECTED
        assert "another payload" in outcome.observation.observation.diagnostic


async def test_job_observations_increase_strictly_and_stale_replays_are_not_fresh() -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        seen: list[Observation] = []
        for index in range(4):
            got = await run(w, query(InspectOwnedJob, f"inspect-{index}", resource))
            target = got.observation.target
            assert target is not None
            seen.append(target.observation)
        sequences = [row.sequence for row in seen]
        assert sequences == sorted(set(sequences))
        assert all(row.request_id.root == "sub" for row in seen)
        for later in range(1, len(seen)):
            verdict = fresh_observation(tuple(seen[:later]), seen[later], complete=True)
            assert isinstance(verdict, Proven)
            # The same facts delivered again, or an older row after a newer one, never rebind history.
            assert isinstance(
                fresh_observation(tuple(seen[: later + 1]), seen[later], complete=True), Proven
            )
            stale = fresh_observation(tuple(seen[: later + 1]), seen[later - 1], complete=True)
            assert not isinstance(stale, Proven)


async def test_retry_after_unknown_is_fresh_to_the_core() -> None:
    async with world() as w:
        lease = FakeLease()
        lease.valid = False
        requests, _ = w.requests()
        sub = submission(candidate=w.stack.snapshot)
        unknown = await requests.execute(sub, context_for(sub, lease))
        first = unknown.observation.observation
        assert first.status is ObservationStatus.UNKNOWN
        assert w.cluster.submissions == []
        retry = await requests.execute(sub, context_for(sub))
        second = retry.observation.observation
        assert second.status is ObservationStatus.PENDING
        assert second.sequence > first.sequence
        assert isinstance(fresh_observation((first,), second, complete=True), Proven)
        resource = resource_of(retry)
        await settled(w, resource)
        inspected = await run(w, query(InspectOwnedJob, "inspect", resource))
        target = inspected.observation.target
        assert target is not None
        assert isinstance(
            fresh_observation((first, second), target.observation, complete=True), Proven
        )


# authority


async def test_lost_lease_gives_unknown_and_no_effect_for_every_effectful_request() -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        lease = FakeLease()
        lease.valid = False
        for request in (
            submission("fresh", candidate=w.stack.snapshot),
            query(CancelOwnedJob, "cancel", resource),
            close_request(),
        ):
            requests, _ = w.requests()
            got = await requests.execute(request, context_for(request, lease))
            assert got.observation.observation.status is ObservationStatus.UNKNOWN
            assert got.owner_events == ()
        assert w.cluster.submissions == [resource.root]
        assert w.cluster.cancelled == []
        project_scope = (await run(w, query(InspectOwnedJob, "still", resource))).observation
        assert project_scope.target is not None
        assert not project_scope.target.observation.terminal


async def test_stale_host_epoch_is_refused_before_the_effect() -> None:
    async with world() as w:
        requests, _ = w.requests()
        warm = submission("warm", candidate=w.stack.snapshot)
        await requests.execute(warm, context_for(warm, epoch=3))
        late = submission("late", candidate=w.stack.snapshot)
        got = await requests.execute(late, context_for(late, epoch=2))
        assert got.observation.observation.status is ObservationStatus.UNKNOWN
        assert len(w.cluster.submissions) <= 1


# ownership


async def test_foreign_and_unknown_resources_are_rejected_without_effect() -> None:
    cluster = ScenarioCluster()
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        for kind in (ObserveOwnedJob, InspectOwnedJob, CollectEvidence, CancelOwnedJob):
            foreign = query(kind, f"foreign-{kind.__name__}", ResourceId(root="someone-elses"))
            got = await run(w, foreign)
            assert got.observation.observation.status is ObservationStatus.REJECTED
            assert got.owner_events == ()
        assert cluster.cancelled == []


async def test_job_of_another_scope_is_not_owned() -> None:
    async with world() as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        other = InspectOwnedJob(
            request_id=RequestId(root="other-scope"),
            scope=SCOPE.model_copy(update={"generation": SCOPE.generation + 1}),
            admission_id=ADMISSION,
            deadline_at=100.0,
            resource_id=resource,
        )
        got = await run(w, other)
        assert got.observation.observation.status is ObservationStatus.REJECTED


# cancel and close


async def test_cancel_is_followed_by_an_inspection_and_reports_what_it_saw() -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        got = await run(w, query(CancelOwnedJob, "cancel", resource))
        own = got.observation.observation
        assert own.status is ObservationStatus.CANCELLED
        assert own.released == (own.diagnostic == "")
        assert cluster.cancelled


async def test_close_fences_the_episode_and_lists_children() -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    async with world(cluster) as w:
        resource = resource_of(await submit(w))
        await settled(w, resource)
        closed = await run(w, close_request())
        own = closed.observation.observation
        assert own.children == (resource,)
        assert own.children_complete
        late = await submit(w, "late")
        assert late.observation.observation.status is ObservationStatus.REJECTED
        assert w.cluster.submissions == [resource.root]


async def test_close_releases_a_job_whose_submission_never_reached_the_executor() -> None:
    """The scope index names the handle before the submit; a crash there must not wedge close."""
    async with world() as w:
        sub = submission(candidate=w.stack.snapshot)
        # Die right after the scope index recorded the handle, before the job record.
        requests, _ = w.requests(1)
        with pytest.raises(CrashError):
            await requests.execute(sub, context_for(sub))
        assert w.cluster.submissions == []
        closed = await run(w, close_request())
        own = closed.observation.observation
        assert own.released
        assert len(own.children) == 1
        # The released close is sealed, and the retried submission is refused for good.
        assert await run(w, close_request()) == closed
        retried = await run(w, sub)
        assert retried.observation.observation.status is ObservationStatus.REJECTED
        assert w.cluster.submissions == []


async def test_a_revision_the_workspace_named_is_measurable() -> None:
    """The workspace executor's reference for a real commit must be accepted as a candidate."""
    async with world() as w:
        sub = submission(
            override=plan(candidate=w.stack.snapshot).model_copy(
                update={"candidate": revision_ref(w.stack.snapshot)}
            )
        )
        got = await run(w, sub)
        assert got.observation.observation.accepted
        assert got.observation.observation.resource_id is not None


@settings(
    max_examples=25, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(
    digest=st.one_of(
        st.just("sha256:" + "a" * 64),
        st.just("a" * 64),
        st.text(min_size=1, max_size=20),
        st.just("git-commit:other"),
    )
)
async def test_a_revision_whose_digest_is_not_its_own_git_commit_is_rejected(digest: str) -> None:
    async with world() as w:
        stale = RevisionRef(revision_id=RevisionId(root=w.stack.snapshot), digest=digest)
        sub = submission(
            override=plan(candidate=w.stack.snapshot).model_copy(update={"candidate": stale})
        )
        got = await run(w, sub)
        assert got.observation.observation.status is ObservationStatus.REJECTED
        assert "git commit" in got.observation.observation.diagnostic
        assert w.cluster.submissions == []


async def test_an_accepted_submission_delivers_the_jobs_first_observation() -> None:
    """Core polls a live job after each job observation, so the first one starts the cycle."""
    async with world() as w:
        got = await submit(w)
        events = [type(event).__name__ for event in got.owner_events]
        assert events == ["MeasurementSubmissionObserved", "JobObserved"]
        job = got.owner_events[1]
        assert isinstance(job, JobObserved)
        assert job.observation == got.observation.observation


async def test_closing_an_older_episode_does_not_fence_a_newer_one() -> None:
    async with world() as w:
        await run(w, close_request("close-old", DecisionId(root="admission-0")))
        got = await submit(w)
        assert got.observation.observation.accepted


# crash at every boundary


async def _boundary_scenarios(
    w: World,
) -> dict[str, EvaluationRoleRequest]:
    resource = resource_of(await submit(w, "seed"))
    await settled(w, resource)
    return {
        "submit": submission("again", candidate=w.stack.snapshot),
        "observe": query(ObserveOwnedJob, "observe", resource),
        "inspect": query(InspectOwnedJob, "inspect", resource),
        "collect": query(CollectEvidence, "collect", resource),
        "cancel": query(CancelOwnedJob, "cancel", resource),
        "close": close_request(),
    }


@pytest.mark.parametrize("name", ["submit", "observe", "inspect", "collect", "cancel", "close"])
async def test_crash_at_every_write_boundary_recovers_exactly_once(name: str) -> None:
    async with world() as probe:
        request = (await _boundary_scenarios(probe))[name]
        _, store = probe.requests()
        requests, store = probe.requests()
        baseline = await requests.execute(request, context_for(request))
        writes = store.writes
        want = baseline.observation.observation
    assert writes > 0
    for crash_at in range(writes + 1):
        async with world() as w:
            request = (await _boundary_scenarios(w))[name]
            before = list(w.cluster.submissions)
            requests, store = w.requests(crash_at)
            try:
                await requests.execute(request, context_for(request))
            except CrashError:
                assert crash_at < writes
            else:
                assert crash_at == writes
            recovered = await run(w, request)
            got = recovered.observation.observation
            if name == "submit" and got.resource_id is not None:
                await settled(w, got.resource_id)
            assert got.status is want.status
            assert got.accepted is want.accepted
            assert got.terminal is want.terminal
            assert got.request_id == want.request_id
            assert got.sequence >= 0
            assert w.cluster.submissions == [
                *before,
                *(s for s in w.cluster.submissions if s not in before),
            ]
            assert len(w.cluster.submissions) == len(set(w.cluster.submissions))
            assert len(w.cluster.submissions) == len(before) + (1 if name == "submit" else 0)
            # A second recovery is a replay of the first.
            assert await run(w, request) == recovered or got.status is ObservationStatus.UNKNOWN


async def test_crash_between_submission_and_seal_does_not_resubmit() -> None:
    async with world() as w:
        sub = submission(candidate=w.stack.snapshot)
        _, probe = w.requests()
        # Die at the last write: the job was submitted, the result was not sealed.
        async with world() as other:
            requests, store = other.requests()
            await requests.execute(submission(candidate=other.stack.snapshot), context_for(sub))
            last = store.writes - 1
        requests, _ = w.requests(last)
        with pytest.raises(CrashError):
            await requests.execute(sub, context_for(sub))
        resumed = await run(w, sub)
        await settled(w, resource_of(resumed))
        assert len(w.cluster.submissions) == 1
        del probe


async def test_wiring_binds_every_evaluation_request_and_core_accepts_the_facts() -> None:
    async with world() as w:
        requests, _ = w.requests()
        executors = RequestExecutors(evaluation=requests)
        resource = resource_of(await submit(w))
        await settled(w, resource)
        for request in (
            submission("wired", candidate=w.stack.snapshot),
            query(ObserveOwnedJob, "w-observe", resource),
            query(InspectOwnedJob, "w-inspect", resource),
            query(CollectEvidence, "w-collect", resource),
            query(CancelOwnedJob, "w-cancel", resource),
            close_request("w-close"),
        ):
            assert executors.refusal(request) is None
        inspected = await run(w, query(InspectOwnedJob, "facts", resource))
        target = inspected.observation.target
        assert target is not None
        assert target.evaluation_result is not None
        target.evaluation_result.validate_observation(target.observation)


# Observation contract: core accepts every output across retries and restarts ---------------


async def _submit_after_unknown() -> list[ExecutionResult]:
    async with world() as w:
        sub = submission(candidate=w.stack.snapshot)
        lost = FakeLease()
        lost.valid = False
        requests, _ = w.requests()
        unknown = await requests.execute(sub, context_for(sub, lost))
        assert unknown.observation.observation.status is ObservationStatus.UNKNOWN
        retried = await run(w, sub)
        await settled(w, resource_of(retried))
        return [unknown, retried, await run(w, sub)]


def _polled(
    kind: type[
        ObserveOwnedJob | InspectOwnedJob | CollectEvidence | CancelOwnedJob | CloseAttemptScope
    ],
) -> Callable[[], Awaitable[list[ExecutionResult]]]:
    """Submit a slow job, then issue the request three times, each on a restarted executor."""

    async def scenario() -> list[ExecutionResult]:
        cluster = ScenarioCluster()
        cluster.states = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
        async with world(cluster) as w:
            submitted = await submit(w)
            resource = resource_of(submitted)
            await settled(w, resource)
            results = [submitted]
            for _ in range(3):
                if issubclass(kind, CloseAttemptScope):
                    results.append(await run(w, close_request()))
                else:
                    results.append(await run(w, query(kind, "request", resource)))
            return results

    return scenario


OBSERVATION_SCENARIOS: dict[type, Callable[[], Awaitable[list[ExecutionResult]]]] = {
    SubmitMeasurement: _submit_after_unknown,
    ObserveOwnedJob: _polled(ObserveOwnedJob),
    InspectOwnedJob: _polled(InspectOwnedJob),
    CollectEvidence: _polled(CollectEvidence),
    CancelOwnedJob: _polled(CancelOwnedJob),
    CloseAttemptScope: _polled(CloseAttemptScope),
}


async def test_every_evaluation_request_kind_has_an_observation_scenario() -> None:
    routed = {kind for kind, role in REQUEST_DISPATCH.items() if role is ExecutorRole.EVALUATION}
    assert set(OBSERVATION_SCENARIOS) == routed


@pytest.mark.parametrize("kind", list(OBSERVATION_SCENARIOS), ids=lambda kind: kind.__name__)
async def test_core_accepts_every_observation_across_retries_and_restarts(kind: type) -> None:
    # Closing seals on its first complete poll, so it has replays but no changed result.
    assert_core_accepts(
        await OBSERVATION_SCENARIOS[kind](), expect_retry=kind is not CloseAttemptScope
    )
