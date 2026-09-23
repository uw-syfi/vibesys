"""Selection and isolation contracts for the orchestration boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationRegistry,
    ProfilerKind,
    VibeSysRuntime,
    built_in_orchestrations,
    create_session,
)
from vibesys.api._dispatch import dispatch_loop
from vibesys.api.contracts import LoopKind, RunRequest, RunResult, RunStatus
from vibesys.api.entry import default_request
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.events import CoreEvent, CoreEventType, RunStartedData
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, Project

if TYPE_CHECKING:
    from pathlib import Path

_EXAMPLE = "examples/model-serving/whisper-large-v3"


class _StubOrchestration:
    def __init__(self, *, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[RunRequest, VibeSysRuntime]] = []

    def execute(self, request: RunRequest, runtime: VibeSysRuntime) -> bool:
        self.calls.append((request, runtime))
        return self.result


def test_registry_rejects_duplicate_and_missing_ids() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=True)
    registry.register(LoopKind.PLAIN, implementation)

    assert registry.resolve(LoopKind.PLAIN) is implementation
    with pytest.raises(ValueError, match="already registered"):
        registry.register(LoopKind.PLAIN, _StubOrchestration(result=False))
    with pytest.raises(ValueError, match="not registered"):
        registry.resolve(LoopKind.EVOLVE)


@pytest.mark.parametrize("invalid_id", ["", " Team", "team/search", "TEAM", "a" * 129])
def test_registry_rejects_ids_outside_descriptor_envelope(invalid_id: str) -> None:
    registry = OrchestrationRegistry()
    with pytest.raises(ValueError, match="invalid orchestration ID"):
        registry.register(invalid_id, _StubOrchestration(result=True))

    registry.register("team.v2", _StubOrchestration(result=True))
    assert registry.resolve("team.v2") is not None


def test_dispatch_uses_injected_registry_without_built_in_loop_calls() -> None:
    registry = OrchestrationRegistry()
    implementation = _StubOrchestration(result=False)
    registry.register(LoopKind.EVOLVE, implementation)
    request = RunRequest.model_construct(loop=LoopKind.EVOLVE)
    integration = LocalRunIntegration()

    assert dispatch_loop(request, integration, registry) is False
    assert implementation.calls[0][0] is request
    assert implementation.calls[0][1] is not integration


def test_builtin_ids_resolve_to_their_own_implementations() -> None:
    registry = built_in_orchestrations()

    assert registry.resolve(LoopKind.AGENT) is registry.resolve(LoopKind.PROFILE_GUIDED)
    assert registry.resolve(LoopKind.PLAIN) is not registry.resolve(LoopKind.EVOLVE)
    assert registry.resolve(LoopKind.AGENT) is not registry.resolve(LoopKind.PLAIN)


def _custom_request(tmp_path: Path) -> RunRequest:
    project_root = tmp_path / "custom-project"
    project_root.mkdir()
    (project_root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (project_root / "queue.py").write_text("VALUE = 1\n")
    (project_root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="team-search", config_version=2, options={"workers": 3}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        exp_name="custom-run",
        run_environment=RunEnvironmentSpec("local"),
        agent_backend="stub",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_session_executes_unknown_registered_id_and_reports_it(tmp_path: Path) -> None:
    request = _custom_request(tmp_path)
    implementation = _StubOrchestration(result=True)
    registry = OrchestrationRegistry()
    registry.register("team-search", implementation)
    events: list[CoreEvent] = []

    def record(event: CoreEvent) -> None:
        events.append(event)

    session = create_session(request, sink=record, registry=registry)

    session.start()
    result = asyncio.run(session.await_result())
    view = session.view()

    assert implementation.calls[0][0] is request
    assert implementation.calls[0][0].orchestration == request.orchestration
    assert result.run_id.endswith("-custom-run")
    assert view.run_id == result.run_id
    assert result.loop == "team-search"
    assert result.succeeded is True
    assert view.loop == "team-search"
    assert view.status is RunStatus.COMPLETED
    started = next(event for event in events if event.type is CoreEventType.RUN_STARTED)
    assert isinstance(started.data, RunStartedData)
    assert started.data.outer_loop == "team-search"
    assert started.data.expected_roles == ()
    assert Project.open(request.project_root).state.load_run(result.run_id).schema_version == 4


def test_custom_selection_validates_descriptor_and_preserves_builtin_enum(
    tmp_path: Path, repo_root: Path
) -> None:
    custom = _custom_request(tmp_path)
    assert custom.orchestration_id == "team-search"
    assert custom.orchestration is not None
    assert custom.orchestration.options == {"workers": 3}
    assert (
        RunRequest.model_validate_json(custom.model_dump_json()).orchestration_id == "team-search"
    )

    built_in = default_request(Project.open(repo_root / _EXAMPLE), LoopKind.AGENT)
    assert built_in.loop is LoopKind.AGENT
    assert built_in.selected_loop is LoopKind.AGENT
    built_in_result = RunResult(run_id="builtin", loop=LoopKind.AGENT, succeeded=True)
    assert RunResult.model_validate_json(built_in_result.model_dump_json()).loop is LoopKind.AGENT

    values = built_in.model_dump()
    with pytest.raises(ValidationError, match="select exactly one"):
        RunRequest.model_validate({**values, "orchestration": custom.orchestration})
    with pytest.raises(ValidationError, match="select exactly one"):
        RunRequest.model_validate({**values, "loop": None})
    with pytest.raises(ValidationError, match="built-in orchestration IDs"):
        RunRequest.model_validate(
            {
                **values,
                "loop": None,
                "orchestration": OrchestrationDescriptor(id="agent", config_version=1, options={}),
            }
        )
