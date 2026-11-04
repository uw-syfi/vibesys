"""A submission that reuses an earlier run reads that run's ended job and launches nothing (#2949).

Core decides the reuse and names the run in ``SubmitMeasurement.reuses``; the runtime answers
with the earlier job's records, ledgered under the reusing submission and marked with the run
they came from. These tests drive the EVALUATION role over the production executor stack on a
Fake Slurm cluster.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from vs_core.api import (
    CancelOwnedJob,
    EvidenceKind,
    InspectOwnedJob,
    MeasurementFailure,
    ObservationStatus,
    RequestId,
    SubmitMeasurement,
)
from vs_runtime.api.core import StageSettled
from vs_runtime.api.slurm_evaluation_stack import ADMISSION, SCOPE, plan
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_requests import (
    Recorder,
    World,
    close_request,
    query,
    resource_of,
    run,
    settled,
    world,
)

if TYPE_CHECKING:
    from vs_core.api import EvidenceRef, ResourceId
    from vs_evaluation.api import PollingEvaluationExecutor

pytestmark = pytest.mark.asyncio


class CancelLog:
    """The stack's executor, keeping every handle it was asked to cancel."""

    def __init__(self, inner: PollingEvaluationExecutor) -> None:
        self.inner = inner
        self.cancelled: list[str] = []

    async def cancel(self, handle_id: str) -> None:
        self.cancelled.append(handle_id)
        await self.inner.cancel(handle_id)

    def __getattr__(self, name: str) -> object:
        return getattr(self.inner, name)


def agent_run(w: World, name: str = "agent") -> SubmitMeasurement:
    measured = plan(candidate=w.stack.snapshot).model_copy(update={"purpose": "local-validation"})
    return SubmitMeasurement(
        request_id=RequestId(root=name), scope=SCOPE, admission_id=ADMISSION, plan=measured
    )


def reusing(w: World, source: str, name: str = "official") -> SubmitMeasurement:
    return SubmitMeasurement(
        request_id=RequestId(root=name),
        scope=SCOPE,
        admission_id=ADMISSION,
        plan=plan(candidate=w.stack.snapshot),
        reuses=RequestId(root=source),
    )


def facts(item: EvidenceRef) -> tuple[object, ...]:
    """What a record says, apart from the observation that carried it."""
    return (item.evidence_id, item.kind, item.status, item.artifacts, item.reused_from)


async def ended(w: World, request: SubmitMeasurement) -> tuple[ResourceId, tuple[EvidenceRef, ...]]:
    resource = resource_of(await run(w, request))
    await settled(w, resource)
    target = (
        await run(w, query(InspectOwnedJob, f"inspect-{resource.root}", resource))
    ).observation
    assert target.target is not None
    assert target.target.observation.status is ObservationStatus.SUCCEEDED
    return resource, target.target.evidence


async def test_a_reusing_submission_answers_with_the_earlier_runs_records_and_launches_nothing() -> (
    None
):
    """The reported case: on main the official measurement launched a second Slurm job."""
    async with world() as w:
        source, earlier = await ended(w, agent_run(w))
        heard = Recorder()
        w.observer = heard
        got = await run(w, reusing(w, "agent"))
        resource = resource_of(got)
        assert resource != source
        assert got.observation.observation.status is ObservationStatus.SUCCEEDED
        assert w.cluster.submissions == [source.root], "no second job reached the cluster"
        evidence = got.observation.evidence
        assert [item.evidence_id for item in evidence] == [item.evidence_id for item in earlier]
        assert [item.artifacts for item in evidence] == [item.artifacts for item in earlier]
        assert {item.reused_from for item in evidence} == {RequestId(root="agent")}
        assert {item.purpose for item in evidence} == {"official"}
        assert {item.kind for item in evidence} == {
            EvidenceKind.CORRECTNESS,
            EvidenceKind.BENCHMARK,
        }
        settled_stages = [e for e in heard.heard if isinstance(e, StageSettled)]
        assert {(e.handle_id, e.purpose, e.reused) for e in settled_stages} == {
            (resource.root, "official", True)
        }, "the gates report the reuse"
        again = await run(w, query(InspectOwnedJob, "inspect-again", resource))
        assert again.observation.target is not None
        assert [facts(item) for item in again.observation.target.evidence] == [
            facts(item) for item in evidence
        ]


@pytest.mark.parametrize("source", ["never-submitted", "agent-live"])
async def test_a_reuse_of_a_run_that_is_not_ended_is_refused_as_infrastructure(source: str) -> None:
    """Core then submits afresh, in the open; the runtime never launches in the reuse's place."""
    async with world() as w:
        live: set[str] = set()
        if source == "agent-live":
            w.cluster.states = (SlurmJobStatus.PENDING,)
            resource = resource_of(await run(w, agent_run(w, source)))
            await settled(w, resource)
            live.add(resource.root)
        got = await run(w, reusing(w, source))
        assert got.observation.observation.status is ObservationStatus.REJECTED
        assert got.observation.measurement_failure is MeasurementFailure.INFRASTRUCTURE
        assert set(w.cluster.submissions) == live
        closed = await run(w, close_request())
        children = closed.observation.observation.children
        assert {child.root for child in children} == live, "the refused job is no child"


async def test_cancelling_or_closing_a_reusing_job_never_touches_the_run_it_reads() -> None:
    async with world() as w:
        source, _ = await ended(w, agent_run(w))
        log = CancelLog(w.stack.executor)
        w.executor = cast("PollingEvaluationExecutor", log)
        submitted = reusing(w, "agent")
        resource = resource_of(await run(w, submitted))
        cancel = CancelOwnedJob(
            request_id=RequestId(root="cancel"),
            scope=SCOPE,
            admission_id=ADMISSION,
            resource_id=resource,
        )
        cancelled = await run(w, cancel)
        assert cancelled.observation.observation.released
        closed = await run(w, close_request())
        assert closed.observation.observation.released
        assert set(closed.observation.observation.children) == {source, resource}
        assert resource.root not in log.cancelled
        assert log.cancelled.count(source.root) <= 1, "only the close's own sweep of the scope"
