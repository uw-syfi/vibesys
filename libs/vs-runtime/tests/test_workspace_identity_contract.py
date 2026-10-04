"""Shared workspace identity and ownership contracts over real temporary Git state."""

from __future__ import annotations

import asyncio
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support.run_execution import run_execution_record

from vs_agent.api import NULL_AGENT_EVENT_SINK, NULL_SKILL_SELECTION
from vs_project.api import NullGitTrackerEvents, OrchestrationDescriptor, RunEnvironmentRecord
from vs_runtime.api import (
    RuntimeContractError,
    WorkspaceRestoreError,
    member_workspace_id,
    validate_member_id,
)
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    create_run_control_channel,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionLifecycleSink,
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaces,
)
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from typing import TextIO

    from vs_runtime.api import CandidateWorkspace, Workspaces
    from vs_sandbox.api import Sandbox


type Implementation = Literal["fake", "git"]


@dataclass
class _EnvironmentSession:
    sandbox: Sandbox
    view: RunEnvironmentView

    def __enter__(self) -> _EnvironmentSession:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        pass


def _emit(text: str, writer: TextIO) -> None:
    writer.write(text + "\n")


@asynccontextmanager
async def _workspaces(implementation: Implementation) -> AsyncIterator[Workspaces]:
    with TemporaryDirectory() as scratch, ExitStack() as ownership:
        root = Path(scratch) / "project"
        root.mkdir()
        (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
        if implementation == "fake":
            workspaces = FakeWorkspaces(FakeWorkspace(path=root), supports_parallel_candidates=True)
        else:
            project = ownership.enter_context(
                open_project_run_resources(
                    ProjectRunRequest(
                        project_root=root,
                        run_id="workspace-contract",
                        display_name="workspace contract",
                        task_name=None,
                        existing=False,
                        framework_version="1.2.3",
                        run_environment=RunEnvironmentRecord(name="local"),
                        execution=run_execution_record(),
                        orchestration=OrchestrationDescriptor(
                            id="test-policy", config_version=1, options={}
                        ),
                    ),
                    effects=ProjectRunEffects(
                        git_events=NullGitTrackerEvents(),
                        log_emit=_emit,
                        on_log_ready=lambda _path: None,
                    ),
                    resolve_resume=lambda _manifest: pytest.fail("unexpected resume"),
                )
            )
            environment = open_run_environment_resources(
                RunEnvironmentRequest(
                    log_dir=project.logger.log_dir,
                    workspace=root,
                    ref_dir=None,
                    backend=FakeComputeBackend(),
                    agent_backend="stub",
                    cli_provider="codex",
                    run_id="workspace-contract",
                    framework_root=Path(scratch),
                ),
                lambda _request: _EnvironmentSession(
                    FakeSandbox(),
                    RunEnvironmentView(
                        paths=AgentPaths(), supports_parallel_candidate_evaluation=True
                    ),
                ),
            )
            ownership.callback(environment.close)
            resources = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
            )
            workspaces = create_workspace_runtime(
                (),
                workspace_resources=resources,
                resolve_configuration=lambda _role: pytest.fail("unexpected agent"),
                session_store=lambda: None,
                control=create_run_control_channel(FakeRunControlEventSink()),
                lifecycle_events=FakeAgentExecutionLifecycleSink(),
                agent_events=NULL_AGENT_EVENT_SINK,
                route_message=lambda message, _steering: message,
                blocking=BlockingOperations(),
            ).workspaces
        try:
            yield workspaces
        finally:
            await workspaces.close()


@pytest.mark.parametrize("implementation", ["fake", "git"])
@settings(max_examples=12)
@given(member_id=st.text(max_size=160))
@example(member_id=" H1")
@example(member_id="H1\n")
@example(member_id="e\u0301")
@example(member_id="KV.Cache_v2 / ../Ünïcode")
@example(member_id="UPPER")
def test_member_identity_validation(implementation: Implementation, member_id: str) -> None:
    """CONFORM-1: implementations reject exactly the public validator's malformed IDs."""

    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            try:
                validate_member_id(member_id)
            except ValueError:
                with pytest.raises(ValueError, match="invalid agent member ID"):
                    await workspaces.create_candidate(member_id=member_id)
            else:
                candidate = await workspaces.create_candidate(member_id=member_id)
                assert candidate.id == member_workspace_id(member_id)
                assert candidate.revision == workspaces.root.revision
                await candidate.discard()

    asyncio.run(exercise())


async def _assert_discarded(candidate: CandidateWorkspace, revision: str) -> None:
    for attribute in ("id", "path", "revision", "trusted_input_baseline"):
        with pytest.raises((ValueError, RuntimeContractError), match="closed"):
            getattr(candidate, attribute)
    for operation in (
        lambda: candidate.snapshot("after-discard"),
        lambda: candidate.restore(revision),
        lambda: candidate.try_restore(revision),
        lambda: candidate.retain(revision, label="after-discard"),
        candidate.pending_changes,
    ):
        with pytest.raises((ValueError, RuntimeContractError), match="closed"):
            await operation()


@pytest.mark.parametrize("implementation", ["fake", "git"])
@settings(max_examples=6)
@given(members=st.lists(st.sampled_from(["H1", "h1", "a/b", "a b", ".."]), min_size=1, max_size=6))
@example(members=["H1", "h1", "H1"])
def test_member_ownership_revision_and_reuse(
    implementation: Implementation, members: list[str]
) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            first_revision = await workspaces.root.snapshot("first")
            (workspaces.root.path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
            second_revision = await workspaces.root.snapshot("second")
            assert first_revision != second_revision
            paths: dict[str, Path] = {}
            for member in members:
                candidate = await workspaces.create_candidate(first_revision, member_id=member)
                assert candidate.revision == first_revision
                assert candidate.trusted_input_baseline == workspaces.root.trusted_input_baseline
                candidate_path = candidate.path
                if member in paths:
                    assert candidate_path == paths[member]
                else:
                    assert candidate_path not in paths.values()
                    paths[member] = candidate_path
                with pytest.raises(RuntimeContractError, match="already has a live candidate"):
                    await workspaces.create_candidate(second_revision, member_id=member)
                assert candidate.revision == first_revision
                await candidate.discard()
                await candidate.discard()
                await _assert_discarded(candidate, first_revision)
                replacement = await workspaces.create_candidate(second_revision, member_id=member)
                assert replacement.path == candidate_path
                assert replacement.revision == second_revision
                await _assert_discarded(candidate, first_revision)
                assert replacement.revision == second_revision
                await replacement.discard()

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
def test_anonymous_candidates_have_distinct_live_identities(implementation: Implementation) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            first = await workspaces.create_candidate()
            second = await workspaces.create_candidate()
            assert first.id != second.id
            assert first.path != second.path
            previous_ids = {first.id, second.id}
            previous_paths = {first.path, second.path}
            await first.discard()
            third = await workspaces.create_candidate()
            assert third.id not in previous_ids
            assert third.path not in previous_paths

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
@pytest.mark.parametrize("attribute", ["id", "path", "revision", "trusted_input_baseline"])
def test_discarded_candidate_properties(implementation: Implementation, attribute: str) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            candidate = await workspaces.create_candidate(member_id="discard-regression")
            await candidate.discard()
            with pytest.raises((ValueError, RuntimeContractError), match="closed"):
                getattr(candidate, attribute)

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
@pytest.mark.parametrize(
    "operation", ["snapshot", "restore", "try_restore", "retain", "pending_changes"]
)
def test_discarded_candidate_operations(implementation: Implementation, operation: str) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            candidate = await workspaces.create_candidate(member_id="discard-regression")
            revision = candidate.revision
            assert revision is not None
            await candidate.discard()
            operations = {
                "snapshot": lambda: candidate.snapshot("discarded"),
                "restore": lambda: candidate.restore(revision),
                "try_restore": lambda: candidate.try_restore(revision),
                "retain": lambda: candidate.retain(revision, label="discarded"),
                "pending_changes": candidate.pending_changes,
            }
            with pytest.raises((ValueError, RuntimeContractError), match="closed"):
                await operations[operation]()

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
@settings(max_examples=6)
@given(member_id=st.sampled_from(["H1", "h1", "a/b", "a b", ".."]))
@example(member_id="H1")
def test_candidate_restore_preserves_revision_identity(
    implementation: Implementation, member_id: str
) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            revision = workspaces.root.revision
            assert revision is not None
            candidate = await workspaces.create_candidate(revision, member_id=member_id)
            assert candidate.revision == revision
            with pytest.raises(WorkspaceRestoreError):
                await candidate.restore("0" * 40)
            assert candidate.revision == revision
            assert not await candidate.try_restore("0" * 40)
            assert candidate.revision == revision
            (workspaces.root.path / "candidate.py").write_text("VALUE = 3\n", encoding="utf-8")
            newer_revision = await workspaces.root.snapshot("newer")
            await candidate.discard()
            replacement = await workspaces.create_candidate(newer_revision, member_id=member_id)
            assert replacement.revision == newer_revision
            await replacement.restore(revision)
            assert replacement.revision == revision
            assert await replacement.try_restore(newer_revision)
            assert replacement.revision == newer_revision

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
@settings(max_examples=6)
@given(member_id=st.sampled_from(["H1", "h1", "a/b", "a b", ".."]))
@example(member_id="H1")
def test_revision_mismatch_does_not_acquire_member(
    implementation: Implementation, member_id: str
) -> None:
    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            revision = workspaces.root.revision
            assert revision is not None
            # Workspaces.create_candidate promises no uniform failure type for
            # an unavailable revision. Both failures must leave ownership free.
            with pytest.raises((RuntimeError, CalledProcessError)):
                await workspaces.create_candidate("0" * 40, member_id=member_id)
            candidate = await workspaces.create_candidate(revision, member_id=member_id)
            assert candidate.revision == revision

    asyncio.run(exercise())
