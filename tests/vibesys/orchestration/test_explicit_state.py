"""Plugin-bound typed state behavior over the production run host."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict, Field

from vibesys.api import RunStopped, create_session
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.plugin_catalog import OrchestrationRegistry
from vibesys.run.contracts import ResumeRef, RunRequest
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_project.api import OrchestrationDescriptor, Project
from vs_runtime.api import (
    OrchestrationPlugin,
    PluginProjection,
    ProfileExecution,
    RunHost,
    RunStatus,
    RuntimeContractError,
    StateModelError,
)
from vs_runtime.api.testing import FakeWorkspace

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


def _project_state(state: BaseModel) -> PluginProjection:
    typed = _State.model_validate(state)
    return PluginProjection(payload={"values": typed.values})


PLUGIN = OrchestrationPlugin(
    id="state-probe",
    agents=(),
    options=_Options,
    orchestrate=_orchestrate,
    state=_State,
    project=_project_state,
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
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
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


def test_observer_failure_is_after_durability_and_restart_can_commit(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register_plugin(PLUGIN)

    class _ProjectionError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("projection failed")

    def fail_observer(_view: object, _changed: tuple[str, ...] | None) -> None:
        raise _ProjectionError

    async def first_commit() -> str:
        session = create_session(_request(project_root), sink=_discard_event, registry=registry)
        session.on_committed_view(fail_observer)
        session.start()
        try:
            with pytest.raises(_ProjectionError, match="projection failed"):
                await session.await_result()
            return session.view().run_id
        finally:
            session.close()

    async def resume_and_commit(run_id: str) -> None:
        request = _request(project_root).model_copy(
            update={"resume": ResumeRef(run_id=run_id), "exp_name": None}
        )
        session = create_session(request, sink=_discard_event, registry=registry)
        session.start()
        try:
            result = await session.await_result()
            assert result.succeeded
        finally:
            session.close()

    run_id = asyncio.run(first_commit())
    stored = (
        Project.open(project_root)
        .state.portable_namespace(run_id, PLUGIN.id)
        .slot("state.json", _State)
        .load_optional()
    )
    assert stored == _State(values=[7])
    asyncio.run(resume_and_commit(run_id))


def test_public_session_composes_a_registered_plugin_with_typed_state(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    registry = OrchestrationRegistry()
    registry.register_plugin(PLUGIN)
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
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
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


def test_workspace_restore_leaves_candidate_checkpoint_index_clean(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
            root = run.workspaces.root
            candidate_file = root.path / "queue.py"
            candidate_file.write_text("VALUE = 2\n")
            await run.state.commit(_State(values=[1]), workspace=root, label="candidate one")
            first_candidate = root.revision
            assert first_candidate is not None

            candidate_file.write_text("VALUE = 3\n")
            await run.state.commit(_State(values=[2]), workspace=root, label="candidate two")
            latest_head = root.revision
            assert latest_head is not None

            await root.restore(first_candidate, clean=True)
            assert root.revision == latest_head
            assert candidate_file.read_text() == "VALUE = 2\n"

            await run.state.commit(
                _State(values=[3]), workspace=root, label="checkpoint restored candidate"
            )
            assert await root.pending_changes() == []
            assert await run.state.load(_State) == _State(values=[3])

    try:
        asyncio.run(exercise())
    finally:
        integration.close()


def test_real_state_adapter_rejects_wrong_models_and_non_root_workspaces(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    integration = LocalRunIntegration()

    async def exercise() -> None:
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
            with pytest.raises(StateModelError, match="requires _State, got _OtherState"):
                await run.state.load(_OtherState)
            with pytest.raises(StateModelError, match="requires _State, got _ChildState"):
                await run.state.commit(_ChildState())
            with pytest.raises(RuntimeContractError, match="live root workspace"):
                await run.state.commit(
                    _State(), workspace=FakeWorkspace(path=run.workspaces.root.path)
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
        async with open_product_run_host(request, integration, plugin=PLUGIN):
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
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
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
        async with open_product_run_host(_request(project_root), integration, plugin=PLUGIN) as run:
            facts = run.facts
            assert facts.domain_id == "generic"
            assert facts.environment_notes == ""
            assert facts.profile_execution is ProfileExecution.LOCAL
            assert str(project_root) in facts.objective_location
            assert facts.reference_location == "."
            assert facts.accuracy_command == "true"
            assert facts.benchmark_command == "true"
            assert facts.accuracy_configured
            assert not facts.benchmark_configured
            assert facts.profiler_id == "none"
            assert facts.workspace_sources == ()
            assert run.facts is facts

    try:
        asyncio.run(exercise())
    finally:
        integration.close()
