"""A SkyPilot step's work is cleared by its tag: its agent's processes, then the jobs it asked for.

The Fake is a ``sky`` CLI over in-memory clusters (``FakeSkyPilotCluster``), driven through the real
``SkyPilotJobRunner`` and the bridge's real invocation journal on disk.
"""

from __future__ import annotations

import hashlib
from itertools import count
from typing import TYPE_CHECKING

import pytest

from vs_core.api import RequestId
from vs_project.api import Project
from vs_runtime.api.docker_agent_work_reaper import SkyPilotStepWorkReaper
from vs_runtime.api.step_runner import ClearUnavailableError
from vs_runtime.api.testing import (
    FakeStageWorkReaper,
    StageWorkReaperContract,
    StageWorkUnderTest,
)
from vs_sandbox.api.skypilot import (
    AttemptResourcesRecord,
    InvocationJournal,
    InvocationPhase,
    SkyPilotJobRunner,
    cancel_step_jobs,
    deterministic_job_name,
)
from vs_sandbox.api.testing import FakeSkyPilotCluster
from vs_sim.api.testing import InlineBlockingRunner, VirtualClock, run_virtual

if TYPE_CHECKING:
    from pathlib import Path

_CLUSTER = "vibesys-run-a"
_RESOURCES = AttemptResourcesRecord(
    profile_name="profile",
    infra="infra",
    accelerator_type="H100",
    nodes=1,
    accelerators_per_node=1,
)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class World:
    """A cluster, the bridge's journal over a real state directory, and the reaper over both."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.cluster = FakeSkyPilotCluster()
        self.journal = InvocationJournal(Project.open(root).state.state_store_namespace("skypilot"))
        self.runner = SkyPilotJobRunner(self.cluster, executable="sky")
        self.agents = FakeStageWorkReaper()
        self.reaper = SkyPilotStepWorkReaper(
            self.agents, self.journal, self.runner, InlineBlockingRunner()
        )
        self._invocations = count(1)

    def invoke(
        self,
        tag: str | None,
        *,
        phase: InvocationPhase = InvocationPhase.RUNNING,
        knows_job_id: bool = True,
    ) -> tuple[str, int | None]:
        """The agent of a step asked for an evaluation, which got as far as *phase*.

        Returns the invocation and its job id on the cluster (``None`` before any job exists).
        """
        invocation = f"{next(self._invocations):032x}"
        record = self.journal.prepare(invocation, _digest(invocation), _digest("snapshot"), tag)
        if phase is InvocationPhase.PREPARED:
            return invocation, None
        record = self.journal.submitting(record, _CLUSTER, _RESOURCES)
        job_id = self.cluster.start_job(_CLUSTER, deterministic_job_name(invocation))
        if phase is not InvocationPhase.SUBMITTING and knows_job_id:
            self.journal.submitted(record, job_id, _CLUSTER)
        return invocation, job_id

    def leave(self, tag: RequestId) -> None:
        """A dead agent left a job of *tag* running."""
        self.invoke(tag.root)

    def tagged_jobs_running(self, tag: str) -> int:
        """Jobs on the cluster that have not ended and belong to an invocation of *tag*."""
        return sum(
            self.cluster.state_of(_CLUSTER, job_id).value in {"RUNNING", "PENDING", "INIT"}
            for record in self.journal.records()
            if record.step_tag == tag
            for job_id in self._job_ids(record.job_name)
        )

    def _job_ids(self, job_name: str) -> list[int]:
        queue = self.runner.query_job(_CLUSTER, job_name=job_name)
        return [] if queue is None else [queue.job_id]


class TestSkyPilotStepWorkReaper(StageWorkReaperContract):
    @pytest.fixture(autouse=True)
    def _scratch(self, tmp_path: Path) -> None:
        self._root = tmp_path
        self._worlds = count()

    def stage_work_under_test(self) -> StageWorkUnderTest:
        world = World(self._root / f"world-{next(self._worlds)}")
        clock = VirtualClock(0.0)
        return StageWorkUnderTest(
            reaper=world.reaper,
            run=lambda main: run_virtual(clock, main),
            leave=world.leave,
            running=lambda tag: world.tagged_jobs_running(tag.root),
            unreachable=world.cluster.unreachable,
        )


def _reap(world: World, tag: str) -> None:
    run_virtual(VirtualClock(0.0), world.reaper.reap(RequestId(root=tag)))


def test_the_jobs_of_other_steps_and_of_no_step_keep_running(tmp_path: Path) -> None:
    world = World(tmp_path)
    _, mine = world.invoke("step-a")
    _, longer = world.invoke("step-ab")
    _, untagged = world.invoke(None)

    _reap(world, "step-a")

    assert mine is not None
    assert world.cluster.state_of(_CLUSTER, mine).value == "CANCELLED"
    assert longer is not None
    assert untagged is not None
    assert world.cluster.state_of(_CLUSTER, longer).value == "RUNNING"
    assert world.cluster.state_of(_CLUSTER, untagged).value == "RUNNING"


def test_a_job_whose_id_the_host_died_before_recording_is_found_by_its_name(
    tmp_path: Path,
) -> None:
    """A crash between ``sky exec`` and the record of the job id leaves SUBMITTING with no id."""
    world = World(tmp_path)
    _, job = world.invoke("step-a", phase=InvocationPhase.SUBMITTING)

    _reap(world, "step-a")

    assert job is not None
    assert world.cluster.state_of(_CLUSTER, job).value == "CANCELLED"


def test_an_invocation_that_never_submitted_has_no_job_to_cancel(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.invoke("step-a", phase=InvocationPhase.PREPARED)

    _reap(world, "step-a")

    assert all(call[1] != "cancel" for call in world.cluster.calls)


def test_the_cluster_stays_and_a_reaped_step_is_reaped_again_without_error(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.invoke("step-a")

    _reap(world, "step-a")
    _reap(world, "step-a")

    assert [call[1] for call in world.cluster.calls if call[1] not in {"queue", "cancel"}] == []
    assert world.cluster.running(_CLUSTER) == 0


def test_the_agents_are_ended_before_any_job_is_cancelled(tmp_path: Path) -> None:
    """So nothing the dead turn left can ask the bridge for another job after the sweep."""
    world = World(tmp_path)
    world.invoke("step-a")
    tag = RequestId(root="step-a")
    world.agents.leave(tag)

    _reap(world, "step-a")

    assert world.agents.reaped == [tag]


def test_an_unreachable_agent_container_cancels_no_job(tmp_path: Path) -> None:
    world = World(tmp_path)
    _, job = world.invoke("step-a")
    world.agents.refusing = True

    with pytest.raises(ClearUnavailableError):
        _reap(world, "step-a")

    assert job is not None
    assert world.cluster.state_of(_CLUSTER, job).value == "RUNNING"


def test_cancel_step_jobs_names_the_invocations_it_cancelled(tmp_path: Path) -> None:
    world = World(tmp_path)
    first, _ = world.invoke("step-a")
    second, _ = world.invoke("step-a")
    world.invoke("step-b")

    assert cancel_step_jobs(world.journal, world.runner, "step-a") == (first, second)
    assert cancel_step_jobs(world.journal, world.runner, "step-a") == (first, second)
