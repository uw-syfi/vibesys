"""Product profiler resolution handles host capability failures by request mode."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from vibesys.api import ComputeBackend, Config, ConfigurationError, OrchestrationDescriptor
from vibesys.api.request import ProfilerKind, RunRequest, load_task
from vibesys.orchestration.profilers import ProfilerPreflightResult
from vibesys.run.profilers import resolve_run_profiler
from vs_runtime.api.infrastructure import DockerEnvironment, DockerEnvironmentConfig

if TYPE_CHECKING:
    from pathlib import Path


class _LinuxCpuEnvironment(DockerEnvironment):
    """A real environment policy constrained to the profiler under test."""

    default_profiler_id = ProfilerKind.LINUX_CPU.value
    supported_profiler_ids = frozenset({ProfilerKind.LINUX_CPU.value})


def _request(tmp_path: Path, requested: ProfilerKind) -> RunRequest:
    project = tmp_path / "project"
    project.mkdir()
    write_task(
        project,
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["true"]\n',
        objective="Improve the program.\n",
    )
    return RunRequest(
        project_root=project,
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_task(project),
        exp_name="profiler-resolution",
        profiler_kind=requested,
        backend=ComputeBackend.CPU,
    )


def _unusable_linux_cpu(kind: ProfilerKind) -> ProfilerPreflightResult:
    assert kind is ProfilerKind.LINUX_CPU
    return ProfilerPreflightResult(
        kind,
        usable=False,
        diagnostics=("perf_event_paranoid_restrictive", "perf_stat_unavailable"),
    )


def test_auto_profiler_degrades_to_none_when_native_preflight_fails(tmp_path: Path) -> None:
    request = _request(tmp_path, ProfilerKind.AUTO)
    logs: list[str] = []

    resolved = resolve_run_profiler(
        request,
        _LinuxCpuEnvironment(DockerEnvironmentConfig()),
        native_preflight=_unusable_linux_cpu,
        log=logs.append,
    )

    assert resolved is ProfilerKind.NONE
    assert logs == [
        "[profiler] Resolved profiler 'linux_cpu' is not usable on this host: "
        "perf_event_paranoid_restrictive, perf_stat_unavailable. "
        "--profiler auto is falling back to 'none'."
    ]


def test_explicit_profiler_preserves_typed_preflight_failure(tmp_path: Path) -> None:
    request = _request(tmp_path, ProfilerKind.LINUX_CPU)
    logs: list[str] = []

    with pytest.raises(ConfigurationError) as error:
        resolve_run_profiler(
            request,
            _LinuxCpuEnvironment(DockerEnvironmentConfig()),
            native_preflight=_unusable_linux_cpu,
            log=logs.append,
        )

    assert error.value.diagnostic.code == "profiler_preflight_failed"
    assert error.value.diagnostic.stage == "profiler_preflight"
    assert "perf_event_paranoid_restrictive" in error.value.diagnostic.message
    assert logs == []
