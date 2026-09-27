"""Composition contracts for product workspace resources."""

from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from vibesys.orchestration.workspace_resources import root_workspace_resource
from vs_agent.api import AgentBackend, AgentSpec
from vs_runtime.api.infrastructure import AgentExecutionConfiguration
from vs_runtime.api.testing import FakeAgentExecutionEnvironment
from vs_sandbox.api import HostResource, ProjectPathPolicy

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.context import _RunResources
    from vibesys.events import CoreEventWriter


def test_root_workspace_uses_the_product_environment_opener(tmp_path: Path) -> None:
    opened = FakeAgentExecutionEnvironment(project_path_policy=ProjectPathPolicy())
    resource = HostResource(tmp_path / "input")
    calls: list[dict[str, object]] = []

    def open_environment(**inputs: object) -> FakeAgentExecutionEnvironment:
        calls.append(inputs)
        return opened

    resources = cast(
        "_RunResources",
        SimpleNamespace(
            workspace=tmp_path,
            log_dir=tmp_path / "logs",
            run_log_file=StringIO(),
            run_environment_view=SimpleNamespace(share_agent_session=False),
            device=SimpleNamespace(gpu_env=dict),
            git=SimpleNamespace(current_sha=lambda: "root-revision"),
        ),
    )
    workspace = root_workspace_resource(
        resources,
        (),
        cast("CoreEventWriter", SimpleNamespace()),
        open_environment,
    )

    result = workspace.agent_scope().open_environment(
        AgentExecutionConfiguration(
            "worker",
            AgentSpec(backend=AgentBackend.STUB),
            resources=(resource,),
        )
    )

    assert result is opened
    assert calls == [
        {
            "mounts": (resource,),
            "agent_backend": "stub",
            "cli_provider": "codex",
        }
    ]
