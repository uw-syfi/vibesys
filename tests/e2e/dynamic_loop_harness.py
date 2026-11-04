"""Scenario harness for the scripted run, over the production host.

A scenario runs the way the product does: a CLI-built request goes through
``launch.default_runs`` and the production host composition (real Git worktrees, the
real ``.vibesys`` state store, the runtime's step loop, the trusted evaluation scripts).
The core policy is the built-in ``dynamic`` registration, so the scenarios run the
same catalog the entrypoints use. Two things are Fake because they are external:

- the cluster: ``vs_slurm.fake_connector`` in executing mode runs every production job
  script on this host, so a job finishes at its first poll;
- the agents: the scripted agents of ``tests.e2e.scripted_value_run`` answer each turn.

The input project's benchmark reports ``throughput = VALUE`` from ``queue.py`` and its
accuracy check raises ``ValueError`` when ``VALUE`` is negative, so an agent's edit
decides every trusted outcome.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tests.support.docker_environment import host_container_backend
from tests.support.loop_spec import loop_spec
from tests.support.real_git import REAL_PROCESSES
from tests.support.slurm_environment import with_fake_image_build
from tests.support.task_project import write_task

import launch
from entrypoints.cli import build_run_request, parse_cli_invocation
from launch import LaunchSettings
from vibesys.api import (
    ResumeRef,
    RunRequest,
    RunResult,
    RunStatus,
    RunStopped,
    relaunch,
)
from vibesys.orchestration.dynamic.search.api import RunSnapshot
from vibesys.run.logged_run import read_logged_run
from vs_core.api import CoreState, FlowDriver, FlowState
from vs_project.api import Project
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer
from vs_sim.api import (
    OsThreads,
    SubprocessPipedProcessLauncher,
    SystemClock,
    ThreadBlockingRunner,
    UnixNetwork,
)
from vs_slurm.fake_connector import HOLD_FILE, FakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import Any

    from vibesys.api import CoreEvent, RunHandle
    from vs_agent.api import AgentClientProtocol
    from vs_sim.api import Processes

_BENCHMARK = """\
import json, pathlib, sys
namespace = {}
exec(pathlib.Path("queue.py").read_text(), namespace)
output = sys.argv[sys.argv.index("--vs-output") + 1] if "--vs-output" in sys.argv else "profile-result.jsonl"
value, required = namespace["VALUE"], namespace.get("REQUIRED")
passed = required is None or value >= required
hello = {"kind": "hello", "protocol": 2, "metrics": {"throughput": {"direction": "max"}}}
outcome = (
    {"kind": "result", "values": {"throughput": float(value)}}
    if passed
    else {
        "kind": "error",
        "message": f"warmup stopped: {value}/{required} rounds",
        "partial": {
            "name": "warmup_rounds_per_s",
            "value": value,
            "direction": "max",
            "unit": "rounds/s",
            "target": required,
            "progress": {"completed": value, "required": required, "unit": "rounds"},
        },
    }
)
pathlib.Path(output).write_text("".join(json.dumps(record) + "\\n" for record in (hello, outcome)))
raise SystemExit(0 if passed else 1)
"""

_ACCURACY = """\
import pathlib
namespace = {}
exec(pathlib.Path("queue.py").read_text(), namespace)


def check(value):
    if value < 0:
        raise ValueError(f"queue depth {value} is negative")


check(namespace["VALUE"])
"""

_AGENT_CONFIG = "[evaluation]\nobserve_interval_seconds = 1\nobserve_backoff_cap_seconds = 1\n"


@dataclass(frozen=True, slots=True)
class LoopInput:
    """An input project and the Fake cluster its run evaluates on."""

    root: Path
    cluster: Path
    slurm_config: Path
    agent_config: Path
    connector: FakeConnector
    """The in-process Fake cluster; pass it to :func:`run_request` as ``slurm_processes``."""
    plugin_id: str = "dynamic"
    """The strategy under test, a key of `tests.support.loop_spec.LOOPS`."""

    @classmethod
    def create(
        cls,
        base: Path,
        *,
        poll_interval_s: float = 1.0,
        agent_config: str = _AGENT_CONFIG,
    ) -> LoopInput:
        """Write the input project, the executing cluster, and its Slurm config.

        ``agent_config`` is the operator's agent.toml.
        """
        root = base / "project"
        root.mkdir(parents=True)
        (root / "queue.py").write_text("VALUE = 1\n", encoding="utf-8")
        task = write_task(
            root,
            'version = 1\n[agent]\ndomain = "generic"\n'
            '[accuracy]\ncommand = ["python", ".vibesys/tasks/default/accuracy.py"]\n'
            "timeout_seconds = 30\n"
            '[benchmark]\ncommand = ["python", ".vibesys/tasks/default/benchmark.py"]\n'
            "timeout_seconds = 30\nresult_protocol = 2\n",
            objective="Raise queue throughput.\n",
        )
        (task / "benchmark.py").write_text(_BENCHMARK, encoding="utf-8")
        (task / "accuracy.py").write_text(_ACCURACY, encoding="utf-8")
        connector = FakeConnector(base / "cluster")
        cluster = connector.state
        remote = base / "remote"
        remote.mkdir()
        # The command is never run: the run's evaluations call ``connector`` in process.
        command = json.dumps(["fake-connector"])
        config = base / "slurm.toml"
        config.write_text(
            "[slurm]\n"
            'name = "fake"\n'
            f'remote_workspace_root = "{remote}"\n'
            f"poll_interval_seconds = {poll_interval_s}\n"
            # Each evaluation holds its node alone, so an agent's run may stand for the
            # official one (``SlurmConfig.dedicated_measurement_resources``).
            'sbatch_arguments = ["--exclusive"]\n'
            f'transport = {{ kind = "connector", command = {command} }}\n',
            encoding="utf-8",
        )
        agent_config_file = base / "agent.toml"
        agent_config_file.write_text(agent_config, encoding="utf-8")
        return cls(
            root,
            cluster,
            config,
            agent_config_file,
            connector,
        )

    @classmethod
    def attach(cls, base: Path) -> LoopInput:
        """Open the input :meth:`create` wrote under ``base`` in another process.

        The run's durable records are the product's ``.vibesys`` on disk, which is all
        two processes can share; the cluster is the one directory both execute on.
        """
        connector = FakeConnector(base / "cluster")
        return cls(
            base / "project",
            connector.state,
            base / "slurm.toml",
            base / "agent.toml",
            connector,
        )

    def project(self) -> Project:
        """Open the input project."""
        return Project.open(self.root)

    def hold_jobs(self) -> None:
        """Leave every job submitted from now on pending until it is cancelled."""
        (self.cluster / HOLD_FILE).touch()

    def request(self, **flags: float | str) -> RunRequest:
        """Build the run request from the command line, as the operator's CLI does.

        ``flags`` are long options without the dashes, underscores for hyphens:
        ``max_rounds=2`` is ``--max-rounds 2``. The run environment is the Fake Slurm
        cluster unless ``run_environment`` says otherwise. Its agents run in containers
        that execute on this host.
        """
        chosen: dict[str, float | str] = {
            "max_rounds": 1,
            "max_in_flight": 1,
            **flags,
        }
        argv = [
            "--outer-loop", loop_spec(self.plugin_id).outer_loop,
            "--input", str(self.root),
            "--config", str(self.agent_config),
            "--profiler", "none",
            "--backend", "cpu",
        ]  # fmt: skip
        slurm = "run_environment" not in chosen
        if slurm:
            argv += ["--run-environment", "slurm", "--slurm-config", str(self.slurm_config)]
        for name, value in chosen.items():
            argv += [f"--{name.replace('_', '-')}", str(value)]
        request = build_run_request(parse_cli_invocation(argv))
        return with_fake_image_build(request) if slurm else request


@dataclass
class LoopRun:
    """The outcome of one run over a :class:`LoopInput`."""

    run_id: str
    result: RunResult | None
    error: BaseException | None
    events: list[CoreEvent]

    @property
    def succeeded(self) -> bool | None:
        """Return whether the run reported success; None when it raised."""
        return self.result.succeeded if self.result is not None else None

    @property
    def status(self) -> RunStatus | None:
        """Return the run's reported status; None when it raised."""
        return self.result.status if self.result is not None else None

    def raise_error(self) -> None:
        """Raise the error the run ended with, if any (for a gap's one expected symptom)."""
        if self.error is not None:
            raise self.error


def run_request(
    request: RunRequest,
    *,
    client_factory: Callable[..., AgentClientProtocol],
    relaunch_saved: bool = False,
    on_handle: Callable[[RunHandle], None] | None = None,
    slurm_processes: Processes | None = None,
) -> LoopRun:
    """Execute a built request through the production host composition, on the wall clock."""
    runs = launch.default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            agent_client_factory=client_factory,
            backend_factory=host_container_backend,
            stop_timer=FakeStopTimer(threads=OsThreads()),
            timing=RunTiming(SystemClock(), OsThreads()),
            # The agent's container mounts the repository's `.git` read-only, so the
            # repository must exist on disk: these runs use the product's real Git.
            processes=REAL_PROCESSES,
            piped_processes=SubprocessPipedProcessLauncher(),
            git_repository=None,
            slurm_processes=slurm_processes,
        )
    )
    events: list[CoreEvent] = []

    async def collect(handle: RunHandle) -> None:
        events.extend([event async for event in handle.events()])

    async def run() -> LoopRun:
        if relaunch_saved:
            handle = relaunch(request, runs)
        else:
            handle = runs.resume(request) if request.resume is not None else runs.start(request)
        collector = asyncio.ensure_future(collect(handle))
        if on_handle is not None:
            on_handle(handle)
        try:
            result = await handle.result()
        # lint-waiver: LW-601001 [BLE001]; crash scenarios assert on the run's own
        # > failure together with its events. pytest.raises at each call site would
        # > lose the events and run id the assertions need, and naming one type would
        # > couple the harness to how the host wraps a failure.
        except (Exception, RunStopped) as error:  # noqa: BLE001
            await asyncio.gather(collector, return_exceptions=True)
            return LoopRun(handle.run_id, None, error, events)
        await asyncio.gather(collector, return_exceptions=True)
        return LoopRun(result.run_id, result, None, events)

    return asyncio.run(run())


@dataclass(frozen=True)
class LoadedRun:
    """A run's core state folded from its run.log, and the same with the search's, as JSON."""

    core: CoreState
    envelope: dict[str, Any]
    """``core`` and, once the search logged a whole state, ``strategy``."""
    strategy: FlowState | None = None
    """The search's state as of the last committed record, typed; None before its first whole state."""


def load_run(project: Project, run_id: str, *, plugin_id: str = "dynamic") -> LoadedRun | None:
    """The run as its run.log leaves it, through the one reader; None when it holds no start."""
    logged = read_logged_run(
        project, run_id, registry=loop_spec(plugin_id).registry(), state_type=FlowState
    )
    if logged is None:
        return None
    envelope: dict[str, Any] = {"core": json.loads(logged.core.model_dump_json())}
    if logged.strategy is not None:
        envelope["strategy"] = json.loads(logged.strategy.model_dump_json())
    return LoadedRun(logged.core, envelope, logged.strategy)


def resume_request(request: RunRequest, run_id: str) -> RunRequest:
    """Return ``request`` as a resume of ``run_id``."""
    return request.model_copy(update={"resume": ResumeRef(run_id=run_id), "exp_name": run_id})


def published_snapshot(project: Project, run_id: str) -> RunSnapshot:
    """The record the search published, folded from the run's run.log; fails when it published none."""
    loaded = load_run(project, run_id)
    assert loaded is not None, f"no committed record for {run_id}"
    assert loaded.strategy is not None, f"the search of {run_id} logged no whole state"
    published = FlowDriver.published(loaded.strategy, RunSnapshot)
    assert published is not None, "the search published no record"
    return published
