"""``vibesys compare`` measures a task's comparisons once; a run only reads what was stored."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.cli.compare import describe
from vibesys.comparisons.api import (
    ComparisonHost,
    ComparisonReport,
    measure_comparisons,
    stored_comparisons,
)
from vibesys.inputs import load_task
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
    ComparisonNotMeasured,
)
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster
from vs_sandbox.api.slurm import ComparisonMachine, SlurmExecutionPolicy, SlurmOperatorSettings
from vs_sim.api.testing import NO_PROCESSES, InlineBlockingRunner, ManualClock
from vs_slurm.api import SlurmConfig, SlurmService, SlurmSshTransport

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio

_DIGEST = "d" * 64
_MANIFEST = f"""version = 1
[agent]
domain = "generic"
interface = "service"
[accuracy]
command = ["python", "acc.py"]
[benchmark]
command = ["python", "bench.py"]
[benchmark.result]
json_argument = "--output"
metric = "throughput"

[[comparison]]
name = "vllm"
label = "vLLM"
image = "docker://rocm/vllm@sha256:{_DIGEST}"
serve = ["vllm", "serve", "--port", "${{PORT}}"]
benchmark_args = ["--backend", "vllm"]

[[comparison]]
name = "sglang"
label = "SGLang"
image = "docker://lmsysorg/sglang@sha256:{"e" * 64}"
serve = ["sglang", "--port", "${{PORT}}"]
visible_to_agents = true
"""


def _settings(*, machine: bool = True) -> SlurmOperatorSettings:
    policy = SlurmExecutionPolicy(
        service=SlurmService(
            command=("python", "-m", "engine.server", "VIBESYS_DYNAMIC_PORT"),
            readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1/models",
            startup_timeout_seconds=900,
        ),
        benchmark_arguments=("--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"),
        comparison=(
            ComparisonMachine(
                gpu="rocm", image_cache_dir="/cluster/images", model_cache_dir="/cluster/hf"
            )
            if machine
            else None
        ),
    )
    config = SlurmConfig(
        name="test", remote_workspace_root="/runs", transport=SlurmSshTransport(host="test")
    )
    return SlurmOperatorSettings(config, policy)


class _World:
    def __init__(self, root: Path, state_home: Path) -> None:
        write_task(root, _MANIFEST)
        disks = FakeGitRepositories()
        self.repository = disks.repository(root)
        self.repository.initialize(initial_branch="main")
        self.repository.bind()
        (root / "README.md").write_text("input\n", encoding="utf-8")
        self.repository.stage_all(["."])
        self.repository.commit("input")
        self.root = root
        self.state_home = state_home
        self.cluster = ScenarioCluster()
        self.clock = ManualClock()

    def project(self) -> Project:
        return Project.open(self.root)

    def host(self, scratch: Path) -> ComparisonHost:
        return ComparisonHost(
            clock=self.clock,
            sleeper=self.clock,
            blocking=InlineBlockingRunner(),
            repository=self.repository,
            scratch=scratch,
            processes=NO_PROCESSES,
            cluster=self.cluster,
        )


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _World:
    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    return _World(tmp_path / "project", state_home)


async def test_a_run_shows_not_measured_and_starts_no_job(world: _World) -> None:
    reports = stored_comparisons(world.project(), load_task(world.root), _settings())

    assert [(r.label, r.reading) for r in reports] == [
        ("vLLM", ComparisonNotMeasured()),
        ("SGLang", ComparisonNotMeasured()),
    ]
    assert [r.visible_to_agents for r in reports] == [False, True]
    assert world.cluster.submissions == []


async def test_compare_measures_each_comparison_once_across_nodes_and_a_run_reads_it(
    world: _World, tmp_path: Path
) -> None:
    world.cluster.nodes = ["node-a", "node-b", "node-c"] * 2
    bundle, project = load_task(world.root), world.project()

    reports = await measure_comparisons(
        project, bundle, _settings(), world.host(tmp_path / "scratch")
    )

    measured = {r.name: r.reading for r in reports}
    assert all(isinstance(r, ComparisonMeasured) for r in measured.values())
    vllm = measured["vllm"]
    assert isinstance(vllm, ComparisonMeasured)
    assert len(vllm.samples) == 3
    assert vllm.hosts == ("node-a", "node-b", "node-c")
    assert len(set(world.cluster.submissions)) == 6
    # The baseline's own benchmark arguments reached its jobs, and not the other's.
    flags = ["--backend vllm" in " ".join(b.stages[-1].command) for b in world.cluster.batches]
    assert flags == [True] * 3 + [False] * 3

    shown = stored_comparisons(project, bundle, _settings())
    assert [r.reading for r in shown] == [measured["vllm"], measured["sglang"]]
    assert all(r.cached for r in shown)


async def test_a_stored_reading_is_not_measured_again_unless_forced(
    world: _World, tmp_path: Path
) -> None:
    bundle, project = load_task(world.root), world.project()
    await measure_comparisons(
        project, bundle, _settings(), world.host(tmp_path / "s1"), only=("vllm",)
    )
    submitted = len(world.cluster.submissions)

    again = await measure_comparisons(
        project, bundle, _settings(), world.host(tmp_path / "s2"), only=("vllm",)
    )
    assert again[0].cached is True
    assert len(world.cluster.submissions) == submitted

    forced = await measure_comparisons(
        project, bundle, _settings(), world.host(tmp_path / "s3"), only=("vllm",), force=True
    )
    assert forced[0].cached is False
    assert len(world.cluster.submissions) > submitted


async def test_a_machine_without_the_table_reports_not_configured(
    world: _World, tmp_path: Path
) -> None:
    reports = await measure_comparisons(
        world.project(),
        load_task(world.root),
        _settings(machine=False),
        world.host(tmp_path / "scratch"),
    )

    assert all(isinstance(r.reading, ComparisonNotConfigured) for r in reports)
    assert world.cluster.submissions == []


async def test_a_failing_comparison_is_a_report_not_an_error(world: _World, tmp_path: Path) -> None:
    world.cluster.benchmark_exit = 1

    reports = await measure_comparisons(
        world.project(), load_task(world.root), _settings(), world.host(tmp_path / "scratch")
    )

    assert all(isinstance(r.reading, ComparisonFailed) for r in reports)


async def test_an_unknown_comparison_name_is_refused_naming_it(
    world: _World, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="nope"):
        await measure_comparisons(
            world.project(),
            load_task(world.root),
            _settings(),
            world.host(tmp_path / "scratch"),
            only=("nope",),
        )


def test_each_state_is_one_line_of_words_and_numbers() -> None:
    measured = ComparisonMeasured.model_validate(
        {"metric": "tok", "unit": "tok/s", "samples": [{"value": 10, "host": "n1"}, {"value": 12}]}
    )

    assert describe(ComparisonReport("v", "vLLM", measured)) == (
        "vLLM: 11 tok/s ±12.9% (2 samples nodes n1)"
    )
    assert describe(ComparisonReport("v", "vLLM", ComparisonNotMeasured())) == "vLLM: not measured"
    assert describe(ComparisonReport("v", "vLLM", ComparisonFailed(reason="boom"))) == (
        "vLLM: failed: boom"
    )
