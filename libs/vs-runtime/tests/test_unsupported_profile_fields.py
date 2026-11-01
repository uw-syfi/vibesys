"""A profile request the configured capture cannot satisfy is refused, never left waiting.

The capture here supplies HIP API timing only (a ``timeline`` request). A request that also
names a phase field cannot ever succeed, so the requester must read that as a rejection
that names the fields, and the cluster must never see a job.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_core.api import HostFence, HostId, ObservationStatus, Until
from vs_evaluation.api import EvaluationStep, ProfileField, SemanticEvaluationStage
from vs_project.api import Project
from vs_runtime.api.core import ExecutionContext, KeyedDocuments, MeasurementRequests
from vs_runtime.api.slurm_evaluation_stack import build_stack, plan, submission
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from vs_evaluation.api import (
        AvailabilitySnapshot,
        EvaluationRequest,
        ExecutorObservation,
        ExecutorPoll,
        PollingEvaluationExecutor,
        ResourceRequirements,
    )

pytestmark = pytest.mark.asyncio

SUPPORTED = (ProfileField.HIP_API_TIMING,)
CAPTURE = (
    "python3",
    "rocprof_profiler/remote_capture.py",
    "--request-json",
    json.dumps(
        {
            "kind": "timeline",
            "lifecycle": {"command": "true"},
            "options": {},
            "local_workspace": ".",
        }
    ),
)


def _subsets() -> list[tuple[ProfileField, ...]]:
    fields = tuple(ProfileField)
    return [
        subset
        for size in range(1, len(fields) + 1)
        for subset in itertools.combinations(fields, size)
    ]


class _RequiringFields:
    """The production executor, except that every profile stage names ``fields``.

    This is the request an agent's evaluation tool produces when the profiler asks for
    fields: the stage carries ``required_profile_fields`` down to the executor.
    """

    def __init__(self, inner: PollingEvaluationExecutor, fields: tuple[ProfileField, ...]) -> None:
        self._inner = inner
        self._fields = fields

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        return await self._inner.availability(requirements)

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        steps = []
        for step in request.stages:
            stage = SemanticEvaluationStage.model_validate(step.payload)
            stage = stage.model_copy(update={"required_profile_fields": self._fields})
            steps.append(EvaluationStep(name=step.name, payload=stage.model_dump(mode="json")))
        await self._inner.submit(
            request.model_copy(update={"stages": tuple(steps)}), handle_id=handle_id
        )

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        return await self._inner.inspect_only(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return await self._inner.inspect(handle_id)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        await self._inner.wait_for_change(handle_id, timeout_s)

    async def cancel(self, handle_id: str) -> None:
        await self._inner.cancel(handle_id)

    async def poll(self, handle_id: str) -> ExecutorPoll:
        return await self._inner.poll(handle_id)


@pytest.mark.parametrize("fields", _subsets(), ids="+".join)
async def test_a_request_the_capture_cannot_satisfy_resolves_as_a_rejection_naming_the_fields(
    fields: tuple[ProfileField, ...],
) -> None:
    unsupported = sorted(set(fields) - set(SUPPORTED))
    with tempfile.TemporaryDirectory() as raw:
        base = Path(raw)
        (base / "project").mkdir()
        stack = await build_stack(base / "stack", profile_command=CAPTURE)
        try:
            project = Project.open(base / "project")
            store = KeyedDocuments(project.state.state_store_namespace("run"), threads=SimThreads())
            requests = MeasurementRequests(_RequiringFields(stack.executor, fields), store)
            request = submission(
                "profile",
                override=plan(stages=("profile",), candidate=stack.snapshot),
            )
            context = ExecutionContext(
                fence=HostFence(host_id=HostId(root="host"), epoch=1),
                now_at=5.0,
                payload_digest=hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
                lease=_Lease(),
                until=Until(at=100.0),
            )

            result = await requests.execute(request, context)

            observed = result.observation.observation
            if unsupported:
                assert observed.status is ObservationStatus.REJECTED
                assert observed.terminal
                assert all(field in observed.diagnostic for field in unsupported)
                assert not any(field in observed.diagnostic for field in SUPPORTED)
                assert stack.cluster.submissions == []
            else:
                assert observed.status is not ObservationStatus.REJECTED
        finally:
            await stack.executor.close()


class _Lease:
    def renew(self, *, now_at: float, lease_duration: float) -> None:
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        del now_at
        return True
