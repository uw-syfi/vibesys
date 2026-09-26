"""Plugin-bound typed state behavior over the production run host."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import RunStopped, create_session
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.context import RunSetup
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.orchestration.contracts import OrchestrationRegistry
from vibesys.orchestration.request import RunRequest
from vibesys.orchestration.runtime import RunContext
from vibesys.profilers import ProfilerKind
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import (
    OrchestrationPlugin,
    ProfileExecution,
    RunFacts,
    RunHost,
    RunStatus,
    RuntimeContractError,
    StateModelError,
    WorkspaceRef,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.events import CoreEvent


class _State(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: list[int] = Field(default_factory=list)


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _OtherState(BaseModel):
    value: int


class _ChildState(_State):
    pass


async def _orchestrate(run: RunHost, _options: BaseModel) -> RunStatus:
    await run.state.commit(_State(values=[7]), label="plugin entrypoint")
    return RunStatus.SUCCEEDED


PLUGIN = OrchestrationPlugin(
    id="state-probe",
    agents=(),
    options=_Options,
    orchestrate=_orchestrate,
    state=_State,
)


def _discard_event(event: CoreEvent) -> None:
    del event


def _write_project(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(project_root: Path) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=PLUGIN.id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "state-probe"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="state-probe",
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=ComputeBackend.CPU,
    )


def test_plugin_state_is_deep_copied_persisted_and_published(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()
    published: list[tuple[str, BaseModel]] = []
    integration.add_committed_state_listener(
        lambda namespace, state, _changed: published.append((namespace, state))
    )

    async def exercise() -> str:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=PLUGIN
        ) as run:
            assert await run.state.load(_State) is None
            original = _State(values=[1])
            await run.state.commit(original, label="state only")
            original.values.append(2)

            assert await run.state.load(_State) == _State(values=[1])
            assert published == [(PLUGIN.id, _State(values=[1]))]
            assert published[0][1] is not original
            return run.run_id

    try:
        run_id = asyncio.run(exercise())
    finally:
        integration.close()

    stored = (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", _State)
        .load_optional()
    )
    assert stored == _State(values=[1])


def test_public_session_composes_a_registered_plugin_with_typed_state(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register_plugin(PLUGIN, portable_namespaces=(PLUGIN.id,))
    session = create_session(_request(project_root), sink=_discard_event, registry=registry)
    session.start()

    result = asyncio.run(session.await_result())

    assert result.succeeded
    stored = (
        Project.open(project_root)
        .state.portable_namespace(result.run_id, PLUGIN.id)
        .slot("state.json", _State)
        .load_optional()
    )
    assert stored == _State(values=[7])


def test_state_only_excludes_candidate_edits_and_workspace_commit_includes_them(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=PLUGIN
        ) as run:
            root = run.workspaces.root
            (root.path / "queue.py").write_text("VALUE = 2\n")
            await run.state.commit(_State(values=[1]), label="state only")
            assert "queue.py" in await root.pending_changes()

            await run.state.commit(_State(values=[2]), workspace=root, label="candidate and state")
            assert await root.pending_changes() == []
            assert await run.state.load(_State) == _State(values=[2])

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_real_state_adapter_rejects_wrong_models_and_non_root_workspaces(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=PLUGIN
        ) as run:
            with pytest.raises(StateModelError, match="requires _State, got _OtherState"):
                await run.state.load(_OtherState)
            with pytest.raises(StateModelError, match="requires _State, got _ChildState"):
                await run.state.commit(_ChildState())
            with pytest.raises(RuntimeContractError, match="live root workspace"):
                await run.state.commit(
                    _State(), workspace=WorkspaceRef(path=run.workspaces.root.path)
                )

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_run_context_rejects_a_plugin_other_than_the_selected_orchestration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    request = _request(project_root).model_copy(
        update={
            "orchestration": OrchestrationDescriptor(
                id="different-plugin", config_version=1, options={}
            )
        }
    )
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(request, integration, setup=RunSetup(), plugin=PLUGIN):
            pytest.fail("mismatched plugin was accepted")

    try:
        with pytest.raises(ValueError, match="does not match plugin 'state-probe'"):
            asyncio.run(exercise())
    finally:
        integration.close()


def test_real_control_checkpoint_lands_a_pending_stop(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=PLUGIN
        ) as run:
            await run.control.checkpoint()
            integration.control.request_stop()
            with pytest.raises(RunStopped):
                await run.control.checkpoint()

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_real_run_facts_map_prepared_input_and_environment_once(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with RunContext.open(
            _request(project_root), integration, setup=RunSetup(), plugin=PLUGIN
        ) as run:
            facts = run.facts
            assert facts == RunFacts(
                domain_id="generic",
                environment_notes="",
                profile_execution=ProfileExecution.LOCAL,
            )
            assert run.facts is facts

    try:
        asyncio.run(exercise())
    finally:
        integration.close()
