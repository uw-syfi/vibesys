"""Every environment ends one step's agent processes, or refuses at the start naming what it cannot.

The Docker, SkyPilot, Modal and Slurm environments run the agent in a local container labelled with
the run; the Host environment finds the agent's processes by the tag in their environment. Each
reaper passes the ``StageWorkReaper`` contract over a Fake that models the tool it reaches: a
Docker daemon with its containers, or a process table.
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
from vs_runtime.api.docker_agent_work_reaper import DockerAgentWorkReaper
from vs_runtime.api.evaluation_step_runners import MarkedStageWorkReaper, marked_with_the_tag
from vs_runtime.api.infrastructure import (
    HostEnvironment,
    RunEnvironment,
    SlurmEnvironment,
    build_run_environment,
    make_run_environment_spec,
)
from vs_runtime.api.step_clear import AGENT_STEP_KINDS, StepClearUnsupportedError
from vs_runtime.api.testing import (
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
    VirtualClock,
    run_virtual,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_project.api import StateNamespace
    from vs_runtime.api.infrastructure import EnvironmentSystem

_RUN = "run-a"


def _no_namespace(name: str) -> StateNamespace:
    """A run's local state the environment under test must not read."""
    message = f"the {name} state namespace was read"
    raise AssertionError(message)


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
        mine = _containers(engine, _RUN, 2)
        processes = FakeAgentProcesses()
        clock = VirtualClock(0.0)
        reaper = DockerAgentWorkReaper(
            _RUN, engine, InlineBlockingRunner(), lambda _line: None, processes.reap_tagged
        )
        placed = count()

        def leave(tag: RequestId) -> None:
            processes.start(mine[next(placed) % len(mine)], {STEP_TAG_ENV: tag.root})

        return StageWorkUnderTest(
            reaper=reaper,
            run=lambda main: run_virtual(clock, main),
            leave=leave,
            running=lambda tag: len(processes.alive(tag=tag.root)),
            unreachable=processes.unreachable,
        )


def test_a_run_s_reaper_leaves_the_processes_of_another_run_s_containers(tmp_path: Path) -> None:
    engine = FakeDockerEngine(tmp_path, agent_ids=(1000, 1000), launcher=FakePipedProcessLauncher())
    (mine,) = _containers(engine, _RUN, 1)
    (theirs,) = _containers(engine, "run-b", 1)
    processes = FakeAgentProcesses()
    ours = processes.start(mine, {STEP_TAG_ENV: "step"})
    other = processes.start(theirs, {STEP_TAG_ENV: "step"})
    reaper = DockerAgentWorkReaper(
        _RUN, engine, InlineBlockingRunner(), lambda _line: None, processes.reap_tagged
    )

    run_virtual(VirtualClock(0.0), reaper.reap(RequestId(root="step")))

    assert not processes.is_alive(ours)
    assert processes.is_alive(other)


def _leave(table: FakeProcessTable, tag: RequestId) -> None:
    """A dead agent left a process carrying the tag."""
    table.start(STEP_TAG_ENV, tag.root)


class TestHostAgentWorkReaper(StageWorkReaperContract):
    """The host's agent processes, found by the tag in their environment."""

    def stage_work_under_test(self) -> StageWorkUnderTest:
        table = FakeProcessTable()
        clock = VirtualClock(0.0)
        environment = HostEnvironment(process_table=table)
        reaper = environment.agent_work_reaper(
            _RUN,
            system=idle_environment_system(),
            blocking=InlineBlockingRunner(),
            local_namespace=_no_namespace,
            log=lambda _line: None,
        )

        @contextmanager
        def unreachable() -> Iterator[None]:
            table.unreadable = True
            try:
                yield
            finally:
                table.unreadable = False

        return StageWorkUnderTest(
            reaper=reaper,
            run=lambda main: run_virtual(clock, main),
            leave=lambda tag: _leave(table, tag),
            running=lambda tag: len(table.marked(STEP_TAG_ENV, marked_with_the_tag(tag))),
            unreachable=unreachable,
        )


def test_the_host_reaper_matches_the_whole_tag_and_not_a_longer_one() -> None:
    table = FakeProcessTable()
    kept = table.start(STEP_TAG_ENV, "step-ab")
    ended = table.start(STEP_TAG_ENV, "step-a")
    reaper = MarkedStageWorkReaper(table, marked_with_the_tag)

    run_virtual(VirtualClock(0.0), reaper.reap(RequestId(root="step-a")))

    assert table.alive(kept)
    assert not table.alive(ended)


def test_a_host_that_cannot_list_process_environments_refuses_agent_steps_at_the_start() -> None:
    """The macOS case: ``ps`` does not show another process's environment, so no mark can be found."""
    table = FakeProcessTable()
    table.unreadable = True
    environment = HostEnvironment(process_table=table)

    with pytest.raises(StepClearUnsupportedError) as refused:
        environment.agent_work_reaper(
            _RUN,
            system=idle_environment_system(),
            blocking=InlineBlockingRunner(),
            local_namespace=_no_namespace,
            log=lambda _line: None,
        )

    assert refused.value.environment == "host"
    assert refused.value.step_kinds == AGENT_STEP_KINDS
    assert "dispatch_turn" in str(refused.value)


def _docker_script(spec: ProcessSpec) -> ProcessScript:
    """A Docker client over one container of the run: it lists it, and every other command is ok."""
    argv = spec.argv
    if argv[:2] == ("docker", "ps"):
        return ProcessScript(stdout=b"c0ffee\n")
    return ProcessScript()


def _system(processes: FakeProcesses) -> EnvironmentSystem:
    return replace(idle_environment_system(), processes=processes)


def _docker_world() -> tuple[FakeProcesses, EnvironmentSystem]:
    clock = ManualClock()
    processes = FakeProcesses(script=_docker_script, pass_time=clock.advance)
    return processes, _system(processes)


_SLURM_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
"""


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
        _RUN,
        system=system,
        blocking=InlineBlockingRunner(),
        local_namespace=Project.open(tmp_path).state.state_store_namespace,
        log=lambda _line: None,
    )

    run_virtual(VirtualClock(0.0), reaper.reap(RequestId(root="step-a")))

    listing, sweep = (spec.argv for spec in processes.ran)
    assert listing[:2] == ("docker", "ps")
    assert f"label={RUN_ID_LABEL}={_RUN}" in listing
    assert sweep[:3] == ("docker", "exec", "c0ffee")
    assert sweep[-2:] == (STEP_TAG_ENV, "step-a")


@pytest.mark.parametrize("name", ["docker", "skypilot", "modal", "slurm"])
def test_every_container_environment_answers_the_run_wide_reap(name: str, tmp_path: Path) -> None:
    """Regression: SkyPilot inherited a Docker reap that read Docker settings its config lacks."""
    processes, system = _docker_world()

    _container_environments(tmp_path)[name].reap_orphans(
        _RUN, system=system, log=lambda _line: None
    )

    assert processes.ran[0].argv[:2] == ("docker", "ps")
