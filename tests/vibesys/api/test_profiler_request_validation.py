"""Run profiling requirements fail before public session construction has effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from launch import create_session
from vibesys.api import (
    ComputeBackend,
    Config,
    ConfigurationError,
    CoreEvent,
    OrchestrationDescriptor,
    ProfilerKind,
    ResumeRef,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle

if TYPE_CHECKING:
    from pathlib import Path


def _request(root: Path, *, service: bool, profile: bool, requested: ProfilerKind) -> RunRequest:
    project = root / "project"
    project.mkdir()
    (project / "OBJECTIVE.md").write_text("Improve serving latency.\n", encoding="utf-8")
    manifest = (
        'version = 1\n[agent]\ndomain = "llm-serving"\n'
        '[accuracy]\ncommand = ["python", "check.py"]\n'
        '[benchmark]\ncommand = ["python", "benchmark.py"]\n'
    )
    if profile:
        manifest += '[profile]\ncommand = ["python", "diagnostic.py"]\n'
    (project / "vibesys.input.toml").write_text(manifest, encoding="utf-8")
    config_path = root / "slurm.toml"
    operator_config = (
        '[slurm]\nname = "test-cluster"\nremote_workspace_root = "/remote/runs"\n'
        '[slurm.transport]\nkind = "ssh"\nhost = "cluster"\n'
    )
    if service:
        operator_config += (
            '[vibesys.service]\ncommand = ["python", "serve.py", "--port", '
            '"VIBESYS_DYNAMIC_PORT"]\n'
            'readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"\n'
            "startup_timeout_seconds = 700\n"
        )
    config_path.write_text(operator_config, encoding="utf-8")
    return RunRequest(
        project_root=project,
        input_bundle=load_input_bundle(project),
        orchestration=OrchestrationDescriptor(
            id="multi-agent",
            config_version=1,
            options={
                "interface": "inprocess",
                "max_rounds": 1,
                "max_retries_per_round": 1,
                "judge_every": 1,
                "official_eval_every": 1,
            },
        ),
        config=Config.model_validate(
            {"model": {"name": "gpt-test"}, "agent": {"driver": "agentshim"}}
        ),
        exp_name="profile-preflight",
        runs_dir=root / "runs",
        profiler_kind=requested,
        run_environment=RunEnvironmentSpec("slurm", {"config_path": str(config_path)}),
        backend=ComputeBackend.ROCM,
    )


@pytest.mark.parametrize("requested", [ProfilerKind.AUTO, ProfilerKind.ROCPROF, ProfilerKind.NONE])
@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("resumed", [False, True])
def test_profile_workload_requirement_precedes_provisioning(
    tmp_path: Path, requested: ProfilerKind, *, service: bool, profile: bool, resumed: bool
) -> None:
    """Exhaust the finite requirement matrix for new and resumed sessions."""
    request = _request(tmp_path, service=service, profile=profile, requested=requested)
    if resumed:
        request = request.model_copy(update={"resume": ResumeRef(run_id="prior-run")})
    events: list[CoreEvent] = []
    before = set(tmp_path.rglob("*"))

    def record(event: CoreEvent) -> None:
        events.append(event)

    if service and not profile and requested is not ProfilerKind.NONE:
        with pytest.raises(ConfigurationError, match=r"profile\.command") as error:
            create_session(request, sink=record)
        assert error.value.diagnostic.code == "profile_workload_invalid"
        assert error.value.diagnostic.stage == "profiler_validation"
    else:
        session = create_session(request, sink=record)
        session.close()
    assert events == []
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "requested",
    [
        kind
        for kind in ProfilerKind
        if kind not in {ProfilerKind.AUTO, ProfilerKind.NONE, ProfilerKind.ROCPROF}
    ],
)
def test_incompatible_profiler_is_a_typed_early_configuration_failure(
    tmp_path: Path, requested: ProfilerKind
) -> None:
    request = _request(tmp_path, service=True, profile=True, requested=requested)
    before = set(tmp_path.rglob("*"))

    def discard(event: CoreEvent) -> None:
        del event

    with pytest.raises(ConfigurationError, match="--profiler") as error:
        create_session(request, sink=discard)
    assert error.value.diagnostic.code == "profiler_incompatible"
    assert error.value.diagnostic.stage == "profiler_validation"
    assert set(tmp_path.rglob("*")) == before
