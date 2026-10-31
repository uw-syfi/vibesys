"""Every environment ends one step's agent processes through its own reaper.

The Docker, SkyPilot, Modal and Slurm environments run the agent in a local container labelled with
the run; the Host environment finds the agent's processes through the process table, the way a
local evaluation's stage commands are found. Each reaper passes the ``StageWorkReaper`` contract
over a Fake that models what the tool shows: a daemon with its containers, or a process table that
shows what Linux or macOS shows.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_agent.api.testing import FakeAgentProcesses
from vs_core.api import RequestId
from vs_project.api import Project, RunResourceRequest
from vs_runtime._evaluation_jobs import handle_of_life
from vs_runtime._stage_marks import agent_mark, is_stage_mark_of, stage_mark
from vs_runtime.api.docker_agent_work_reaper import DockerAgentWorkReaper
from vs_runtime.api.infrastructure import (
    HostEnvironment,
    RunEnvironment,
    SlurmEnvironment,
    build_run_environment,
    make_run_environment_spec,
)
from vs_runtime.api.testing import (
    StageWorkPlace,
    StageWorkReaperContract,
    StageWorkUnderTest,
    idle_environment_system,
)
from vs_sandbox.api import RUN_ID_LABEL, STEP_TAG_ENV
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sandbox.fake_docker_engine import FakeDockerEngine
from vs_sim.api import DISCARD, ProcessSpec
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    FakeProcessTable,
    InlineBlockingRunner,
    ManualClock,
    ProcessScript,
    Shows,
    VirtualClock,
    run_virtual,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from contextlib import AbstractContextManager

    from vs_project.api import StateNamespace
    from vs_runtime.api.infrastructure import EnvironmentSystem

RUN = "run-a"
OTHER_RUN = "run-b"


def _no_namespace(name: str) -> StateNamespace:
    """A run's local state the environment under test must not read."""
    message = f"the {name} state namespace was read"
    raise AssertionError(message)


def _run[T](main: Coroutine[object, object, T]) -> T:
    return run_virtual(VirtualClock(0.0), main)


def _containers(engine: FakeDockerEngine, run: str, count_: int) -> tuple[str, ...]:
    return tuple(
        engine.run(
            ["docker", "run", "-d", "--label", f"{RUN_ID_LABEL}={run}", "image"],
            timeout_seconds=1,
            output=DISCARD,
        ).stdout.strip()
        for _ in range(count_)
    )


class TestDockerAgentWorkReaper(StageWorkReaperContract):
    """One step's processes in a run's containers: the Fake daemon lists them, the Fake table kills."""

    @pytest.fixture(autouse=True)
    def _scratch(self, tmp_path: Path) -> None:
        self._root = tmp_path
        self._worlds = count()

    def stage_work_under_test(self) -> StageWorkUnderTest:
        root = self._root / f"world-{next(self._worlds)}"
        root.mkdir()
        engine = FakeDockerEngine(root, agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher())
        containers = {
            RUN: _containers(engine, RUN, 2),
            OTHER_RUN: _containers(engine, OTHER_RUN, 1),
        }
        processes = FakeAgentProcesses()
        reaper = DockerAgentWorkReaper(
            RUN, engine, InlineBlockingRunner(), lambda _line: None, processes.reap_tagged
        )
        placed = count()

        def leave(run: str, tag: RequestId) -> None:
            home = containers[run]
            processes.start(home[next(placed) % len(home)], {STEP_TAG_ENV: agent_mark(run, tag)})

        def running(run: str, tag: RequestId) -> int:
            return len(processes.alive(tag=agent_mark(run, tag)))

        return StageWorkUnderTest(
            reaper=reaper,
            run=_run,
            places=(StageWorkPlace.CONTAINER,),
            leave=lambda tag, _place: leave(RUN, tag),
            leave_in_other_run=lambda tag, _place: leave(OTHER_RUN, tag),
            running=lambda tag: running(RUN, tag),
            running_in_other_run=lambda tag: running(OTHER_RUN, tag),
            unreachable=processes.unreachable,
            stuck=processes.stuck,
        )


class _HostAgents:
    """Agent processes on a host whose process table shows ``shows``, reaped by its environment."""

    def __init__(self, shows: Shows) -> None:
        self.table = FakeProcessTable(shows)
        self.reaper = HostEnvironment(process_table=self.table).agent_work_reaper(
            RUN,
            system=idle_environment_system(),
            blocking=InlineBlockingRunner(),
            local_namespace=_no_namespace,
            log=lambda _line: None,
        )

    def leave(self, run: str, tag: RequestId, place: StageWorkPlace) -> None:
        leader = self.table.start_stage(STEP_TAG_ENV, agent_mark(run, tag))
        if place is StageWorkPlace.DESCENDANT:
            self.table.spawn(leader)

    def running(self, run: str, tag: RequestId) -> int:
        return self.table.running(STEP_TAG_ENV, lambda mark: is_stage_mark_of(run, tag, mark))

    def under_test(self) -> StageWorkUnderTest:
        return StageWorkUnderTest(
            reaper=self.reaper,
            run=_run,
            places=(StageWorkPlace.LEADER, StageWorkPlace.DESCENDANT),
            leave=lambda tag, place: self.leave(RUN, tag, place),
            leave_in_other_run=lambda tag, place: self.leave(OTHER_RUN, tag, place),
            running=lambda tag: self.running(RUN, tag),
            running_in_other_run=lambda tag: self.running(OTHER_RUN, tag),
            unreachable=_flag(self.table, "unreadable"),
            stuck=_flag(self.table, "survivors"),
        )


def _flag(owner: object, name: str) -> Callable[[], AbstractContextManager[None]]:
    @contextmanager
    def context() -> Iterator[None]:
        setattr(owner, name, True)
        try:
            yield
        finally:
            setattr(owner, name, False)

    return context


class TestHostAgentWorkReaperOnLinux(StageWorkReaperContract):
    """The host shows every process's environment, so a descendant is found by its mark."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        return _HostAgents(Shows.ENVIRONMENT).under_test()


class TestHostAgentWorkReaperOnMacOS(StageWorkReaperContract):
    """The host shows only a group leader's arguments, so the agent's leader carries the mark."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        return _HostAgents(Shows.LEADER_ARGUMENTS).under_test()


def test_an_agent_and_a_stage_command_of_one_step_are_found_by_the_same_mark_kind() -> None:
    """One reaper serves both: an agent's mark is the stage mark of the step's own handle."""
    tag = RequestId(root="step-a")

    assert agent_mark(RUN, tag) == stage_mark(RUN, handle_of_life(tag, 0))
    assert is_stage_mark_of(RUN, tag, agent_mark(RUN, tag))
    assert not is_stage_mark_of(OTHER_RUN, tag, agent_mark(RUN, tag))


_SLURM_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
"""


def _docker_script(spec: ProcessSpec) -> ProcessScript:
    """A Docker client over one container of the run: it lists it, and every other command is ok."""
    if spec.argv[:2] == ("docker", "ps"):
        return ProcessScript(stdout=b"c0ffee\n")
    if spec.argv[:2] == ("docker", "exec"):
        return ProcessScript(stdout=b"0\n")
    return ProcessScript()


def _docker_world() -> tuple[FakeProcesses, EnvironmentSystem]:
    clock = ManualClock()
    processes = FakeProcesses(script=_docker_script, pass_time=clock.advance)
    return processes, replace(idle_environment_system(), processes=processes)


def _container_environments(tmp_path: Path) -> dict[str, RunEnvironment]:
    """The environments whose agent runs in a local container labelled with the run."""
    config = tmp_path / "slurm.toml"
    config.write_text(_SLURM_CONFIG, encoding="utf-8")
    resources = RunResourceRequest(accelerators_per_node=1, accelerator_backend="cuda")
    return {
        "docker": build_run_environment(make_run_environment_spec()),
        "skypilot": build_run_environment(
            make_run_environment_spec(
                use_skypilot=True,
                cluster_profile="profile",
                cluster_profiles_file=Path("profiles.toml"),
                resources=resources,
            )
        ),
        "modal": build_run_environment(make_run_environment_spec(use_modal=True)),
        "slurm": SlurmEnvironment(load_slurm_operator_settings(config)),
    }


@pytest.mark.parametrize("name", ["docker", "skypilot", "modal", "slurm"])
def test_a_container_environment_sweeps_the_containers_of_its_run_for_the_step(
    name: str, tmp_path: Path
) -> None:
    processes, system = _docker_world()
    reaper = _container_environments(tmp_path)[name].agent_work_reaper(
        RUN,
        system=system,
        blocking=InlineBlockingRunner(),
        local_namespace=Project.open(tmp_path).state.state_store_namespace,
        log=lambda _line: None,
    )
    tag = RequestId(root="step-a")

    _run(reaper.reap(tag))

    listing, sweep = (spec.argv for spec in processes.ran)
    assert listing[:2] == ("docker", "ps")
    assert f"label={RUN_ID_LABEL}={RUN}" in listing
    assert sweep[:3] == ("docker", "exec", "c0ffee")
    assert sweep[-2:] == (STEP_TAG_ENV, agent_mark(RUN, tag))


@pytest.mark.parametrize("name", ["docker", "skypilot", "modal", "slurm"])
def test_every_container_environment_answers_the_run_wide_reap(name: str, tmp_path: Path) -> None:
    """Regression: SkyPilot inherited a Docker reap that read Docker settings its config lacks."""
    processes, system = _docker_world()

    _container_environments(tmp_path)[name].reap_orphans(RUN, system=system, log=lambda _line: None)

    assert processes.ran[0].argv[:2] == ("docker", "ps")
