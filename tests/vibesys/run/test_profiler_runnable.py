"""Every profiler a run can end up with is runnable in its environment, or refused by name.

A profiler declares the executable it drives and the site of the run where it executes
(`ProfilerDefinition.tool`); the run environment says whether it can run that executable
there (`RunEnvironment.locate_tool`). Run start joins the two, so an unrunnable profiler
is refused with an error naming it, or (for `auto`) not chosen, and never offered and left
to fail in every profiler turn.
"""

from __future__ import annotations

import itertools

import pytest
from tests.support.task_project import write_task

from vibesys.api import ComputeBackend, Config, ConfigurationError, OrchestrationDescriptor
from vibesys.api.request import ProfilerKind, RunRequest, load_task
from vibesys.constants import DomainName
from vibesys.orchestration.profilers import ProfilerSite, profiler_definition
from vibesys.run.profilers import resolve_run_profiler
from vs_runtime.api.infrastructure import (
    DockerEnvironment,
    DockerEnvironmentConfig,
    HostEnvironment,
    RunEnvironment,
    RunEnvironmentSpec,
    SlurmEnvironment,
    ToolSite,
    build_run_environment,
)
from vs_sandbox.api.slurm import load_slurm_operator_settings

_SLURM_SSH = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/vibesys"

[slurm.transport]
kind = "ssh"
host = "test-cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
"""
_SLURM_LOCAL = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["bash", "-c"]
"""
_SLURM_AGENT_GPU = (
    _SLURM_LOCAL
    + """
[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
"""
)
# A refusal at run start names its reason with one of these codes; anything else would be
# an unnamed failure.
_NAMED_REFUSALS = frozenset(
    {
        "profiler_incompatible",
        "profiler_tool_unavailable",
        "profile_workload_invalid",
        "agent_profiler_incompatible",
    }
)


@pytest.fixture(scope="module")
def environments(tmp_path_factory: pytest.TempPathFactory) -> dict[str, RunEnvironment]:
    root = tmp_path_factory.mktemp("operator")
    built: dict[str, RunEnvironment] = {
        "docker": DockerEnvironment(DockerEnvironmentConfig()),
        "host": HostEnvironment(),
        "modal": build_run_environment(RunEnvironmentSpec(name="modal")),
    }
    for name, text in (
        ("slurm-ssh", _SLURM_SSH),
        ("slurm-local", _SLURM_LOCAL),
        ("slurm-agent-gpu", _SLURM_AGENT_GPU),
    ):
        path = root / f"{name}.toml"
        path.write_text(text, encoding="utf-8")
        built[name] = SlurmEnvironment(load_slurm_operator_settings(path))
    return built


@pytest.fixture(scope="module")
def requests(tmp_path_factory: pytest.TempPathFactory) -> dict[DomainName, RunRequest]:
    root = tmp_path_factory.mktemp("projects")
    built: dict[DomainName, RunRequest] = {}
    for domain in DomainName:
        project = root / domain.value
        project.mkdir()
        write_task(
            project,
            f'version = 1\n[agent]\ndomain = "{domain.value}"\n'
            '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n',
        )
        built[domain] = RunRequest(
            project_root=project,
            orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
            config=Config.model_validate({"model": {"name": "gpt-5.5"}}),
            input_bundle=load_task(project),
            exp_name="profiler-runnable",
        )
    return built


def test_a_profiler_is_runnable_where_it_resolves_or_refused_with_a_named_reason(
    environments: dict[str, RunEnvironment], requests: dict[DomainName, RunRequest]
) -> None:
    combinations = itertools.product(environments.items(), DomainName, ComputeBackend, ProfilerKind)
    for (env_name, environment), domain, backend, requested in combinations:
        request = requests[domain].model_copy(
            update={"backend": backend, "profiler_kind": requested}
        )
        logs: list[str] = []
        label = f"{env_name}/{domain.value}/{backend.value}/{requested.value}"
        try:
            resolved = resolve_run_profiler(
                request, environment, native_preflight=None, log=logs.append
            )
        except ConfigurationError as error:
            diagnostic = error.diagnostic
            assert diagnostic.code in _NAMED_REFUSALS, label
            assert diagnostic.message, label
            if diagnostic.code == "profiler_tool_unavailable":
                tool = profiler_definition(requested).tool
                assert tool is not None, label
                assert requested.value in diagnostic.message, label
                assert tool.executable in diagnostic.message, label
            continue
        assert resolved is not ProfilerKind.AUTO, label
        if requested is not ProfilerKind.AUTO and requested is not ProfilerKind.NONE:
            assert resolved is requested, label
        if resolved is ProfilerKind.NONE:
            continue
        tool = profiler_definition(resolved).tool
        if tool is not None:
            assert environment.locate_tool(tool.executable, ToolSite(tool.site.value)).available, (
                label
            )


def test_linux_cpu_is_refused_where_the_editor_container_has_no_perf(
    environments: dict[str, RunEnvironment], requests: dict[DomainName, RunRequest]
) -> None:
    """Regression: under slurm with agent GPU commands, perf was offered and never ran."""
    request = requests[DomainName.GENERIC].model_copy(
        update={"backend": ComputeBackend.CPU, "profiler_kind": ProfilerKind.LINUX_CPU}
    )

    with pytest.raises(ConfigurationError) as refused:
        resolve_run_profiler(request, environments["slurm-agent-gpu"], native_preflight=None)

    assert refused.value.diagnostic.code == "profiler_tool_unavailable"
    message = refused.value.diagnostic.message
    assert "linux_cpu" in message
    assert "`perf`" in message
    assert "editor container" in message


@pytest.mark.parametrize("requested", [ProfilerKind.AUTO])
def test_auto_falls_back_to_none_with_a_logged_reason_when_the_tool_cannot_run(
    environments: dict[str, RunEnvironment],
    requests: dict[DomainName, RunRequest],
    requested: ProfilerKind,
) -> None:
    request = requests[DomainName.GENERIC].model_copy(
        update={"backend": ComputeBackend.CPU, "profiler_kind": requested}
    )
    logs: list[str] = []

    resolved = resolve_run_profiler(
        request, environments["slurm-agent-gpu"], native_preflight=None, log=logs.append
    )

    assert resolved is ProfilerKind.NONE
    assert len(logs) == 1
    assert "`perf`" in logs[0]
    assert "falling back to 'none'" in logs[0]


def test_the_host_locates_a_tool_on_its_path_and_names_the_one_it_lacks() -> None:
    host = HostEnvironment()

    found = host.locate_tool("python3", ToolSite.AGENT_SHELL)
    absent = host.locate_tool("no-such-profiler-executable", ToolSite.GPU_WORKLOAD)

    assert found.available
    assert not absent.available
    assert "no-such-profiler-executable" in absent.reason


def test_policy_sites_are_the_runtime_sites() -> None:
    """Policy names sites it cannot import from the runtime, so each must be a runtime site."""
    assert {site.value for site in ProfilerSite} <= {site.value for site in ToolSite}
