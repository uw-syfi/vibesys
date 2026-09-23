"""`_LocalRunSession.open_agent_environment` on the local (non-sandboxed) path.

Mirrors `tests/server/test_chat_factory.py`'s `_Environment.open` stub: a
fake `RunEnvironment` that records the `RunEnvironmentRequest` it was opened
with, so folding requested mounts into `environment_bind_mounts` can be
asserted without a real sandbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

from vibesys.api.session import _LocalRunSession
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.domains.environment import EnvironmentBindMount
from vibesys.run.integration import RunResourceHandoff
from vibesys.sandbox.run_environment import _cli_container_env, _cli_provider_env_and_auth_files
from vibesys.skills import platform_skill_selection
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


@dataclass(frozen=True)
class _EnvironmentRequest:
    environment_bind_mounts: tuple[EnvironmentBindMount, ...] = ()
    agent_backend: str | None = "cli"
    cli_provider: str | None = "claude"


class _Environment:
    """Records each `RunEnvironmentRequest` it is opened with."""

    def __init__(self) -> None:
        self.requests: list[_EnvironmentRequest] = []

    def open(self, request: _EnvironmentRequest) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            sandbox=SimpleNamespace(agent_path=lambda host: f"/opt/mapped{host}"),
            view=SimpleNamespace(cli_sandboxed=False, isolated=False),
            close=lambda: None,
        )


def _handoff(
    tmp_path: Path,
    environment: _Environment,
    environment_request: _EnvironmentRequest,
    *,
    sandboxed: bool = False,
    project: Any = None,  # noqa: ANN401  # Stand-in for vs_project.Project; only .root is used.
) -> RunResourceHandoff:
    return RunResourceHandoff(
        project=project,
        run_id="run-1",
        workspace=tmp_path / "workspace",
        log_dir=tmp_path / "logs",
        agent_backend="cli",
        driver="agentshim",
        provider="claude",
        model="claude-haiku-4-5",
        role_models=(),
        config=Config.model_validate(
            {
                "model": {"name": "gpt-test"},
                "agent": {"backend": "cli", "driver": "agentshim"},
            }
        ),
        compute_backend=ComputeBackend.CPU,
        skill_source_dirs=(),
        environment=cast("Any", environment),
        environment_request=cast("Any", environment_request),
        run_environment_sandboxed=sandboxed,
        project_path_policy=ProjectPathPolicy(),
        host_resources=(),
    )


def _session_with_handoff(handoff: RunResourceHandoff) -> _LocalRunSession:
    session = object.__new__(_LocalRunSession)
    cast("Any", session)._resource_handoff = handoff  # noqa: SLF001
    return session


def test_open_agent_environment_local_reports_unsandboxed_shape(tmp_path: Path) -> None:
    environment = _Environment()
    handoff = _handoff(tmp_path, environment, _EnvironmentRequest())
    session = _session_with_handoff(handoff)

    result = session.open_agent_environment()

    assert result.backends is None
    assert result.use_docker is False
    assert result.isolated is False
    assert result.config is handoff.config
    # `skill_selection` wraps a fresh closure per call, so compare behavior
    # rather than identity: it must prune exactly what the handoff's compute
    # backend would prune.
    expected_selection = platform_skill_selection(handoff.compute_backend)
    sample_names = ["cpu", "cuda", "trainium", "metal", "unrelated"]
    assert result.skill_selection.skip_dir(
        "pkg/references/platforms", sample_names
    ) == expected_selection.skip_dir("pkg/references/platforms", sample_names)
    result.close()


def test_open_agent_environment_folds_requested_mounts_into_the_request(
    tmp_path: Path,
) -> None:
    environment = _Environment()
    existing_mount = EnvironmentBindMount(tmp_path / "model", "/model", read_only=True)
    handoff = _handoff(tmp_path, environment, _EnvironmentRequest((existing_mount,)))
    session = _session_with_handoff(handoff)
    mount_dir = tmp_path / "extra"
    mount_dir.mkdir()
    requested = HostResource(
        mount_dir,
        HostResourceAccess.READ_ONLY,
        "extra evidence",
        agent_path="/opt/extra",
    )

    result = session.open_agent_environment(mounts=(requested,))

    assert environment.requests[-1].environment_bind_mounts == (
        existing_mount,
        EnvironmentBindMount(mount_dir, "/opt/extra", read_only=True),
    )
    assert result.agent_path(mount_dir) == f"/opt/mapped{mount_dir}"
    result.close()


def test_agent_override_reaches_docker_and_modal_auth_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    environment = _Environment()
    handoff = _handoff(tmp_path, environment, _EnvironmentRequest())
    session = _session_with_handoff(handoff)
    monkeypatch.setattr(
        "vs_agent.api.auth_env_passthrough", lambda provider: {"SELECTED": provider}
    )
    monkeypatch.setattr("vs_agent.api.auth_paths", lambda _provider: ())
    monkeypatch.setattr("vs_agent.api.auth_copy_paths", lambda provider: [(provider, "/auth")])

    opened = session.open_agent_environment(agent_backend="cli", cli_provider="codex")
    selected = environment.requests[-1]
    docker_auth = _cli_container_env(cast("Any", selected))
    modal_env, modal_files = _cli_provider_env_and_auth_files(cast("Any", selected))

    assert selected.agent_backend == "cli"
    assert selected.cli_provider == "codex"
    assert handoff.environment_request.cli_provider == "claude"
    assert docker_auth is not None
    assert docker_auth[0] == "codex"
    assert docker_auth[1]["SELECTED"] == "codex"
    assert modal_env["SELECTED"] == "codex"
    assert modal_files == [("codex", "/auth")]
    opened.close()


def test_investigation_tools_launches_the_chat_tools_server_for_this_run(
    tmp_path: Path,
) -> None:
    environment = _Environment()
    project = SimpleNamespace(root=tmp_path / "project")
    handoff = _handoff(tmp_path, environment, _EnvironmentRequest(), project=project)
    session = _session_with_handoff(handoff)
    result = session.open_agent_environment()

    (server,) = result.investigation_tools()

    assert server.name == "vibesys-run"
    assert server.command == "python"
    assert server.args == (
        "-m",
        "vibesys.api.chat_tools_server",
        "--run-id",
        "run-1",
        "--project-root",
        f"/opt/mapped{project.root}",
    )
    result.close()
