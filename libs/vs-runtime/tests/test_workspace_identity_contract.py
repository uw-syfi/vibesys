"""Shared workspace identity and ownership contracts over real temporary Git state."""

from __future__ import annotations

import asyncio
import os
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from subprocess import CalledProcessError
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal, cast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from tests.support import run_test_command
from tests.support.run_execution import run_execution_record
from tests.support.runtime_operations import VerifyParentRevision as _VerifyRequest

from vs_agent.api import NULL_AGENT_EVENT_SINK, NULL_SKILL_SELECTION
from vs_core.api import HostFence, HostId
from vs_project.api import NullGitTrackerEvents, OrchestrationDescriptor, RunEnvironmentRecord
from vs_runtime.api import (
    RuntimeContractError,
    WorkspaceRestoreError,
    member_workspace_id,
    validate_member_id,
)
from vs_runtime.api.core import ExecutionContext, VerifyRevisionOwner, commit_of, revision_ref
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

    from vs_runtime.api import CandidateWorkspace, RevisionLedger, Workspaces
    from vs_sandbox.api import Sandbox


type Implementation = Literal["fake", "git"]
_IMPLEMENTATIONS: tuple[Implementation, ...] = ("fake", "git")


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


@settings(max_examples=12)
@given(member_id=st.text(max_size=160))
@example(member_id=" H1")
@example(member_id="H1\n")
@example(member_id="e\u0301")
@example(member_id="KV.Cache_v2 / ../Ünïcode")
@example(member_id="UPPER")
def test_member_identity_validation(member_id: str) -> None:
    """CONFORM-1: implementations reject exactly the public validator's malformed IDs."""

    async def exercise(implementation: Implementation) -> None:
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

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


async def _assert_discarded(candidate: CandidateWorkspace, revision: str) -> None:
    for attribute in ("path", "revision", "trusted_input_baseline"):
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


@settings(max_examples=6)
@given(members=st.lists(st.sampled_from(["H1", "h1", "a/b", "a b", ".."]), min_size=1, max_size=6))
@example(members=["H1", "h1", "H1"])
def test_member_ownership_revision_and_reuse(members: list[str]) -> None:
    async def exercise(implementation: Implementation) -> None:
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

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


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
def test_discarded_candidate_keeps_immutable_identity(implementation: Implementation) -> None:
    """Cleanup observers can still identify a released candidate resource."""

    async def exercise() -> None:
        async with _workspaces(implementation) as workspaces:
            candidate = await workspaces.create_candidate(member_id="identity-regression")
            identity = candidate.id
            await candidate.discard()
            assert candidate.id == identity
            replacement = await workspaces.create_candidate(member_id="identity-regression")
            assert replacement.id == identity
            assert candidate.id == identity

    asyncio.run(exercise())


@pytest.mark.parametrize("implementation", ["fake", "git"])
@pytest.mark.parametrize("attribute", ["path", "revision", "trusted_input_baseline"])
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


@settings(max_examples=6)
@given(member_id=st.sampled_from(["H1", "h1", "a/b", "a b", ".."]))
@example(member_id="H1")
def test_candidate_restore_preserves_revision_identity(member_id: str) -> None:
    async def exercise(implementation: Implementation) -> None:
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

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


@settings(max_examples=6)
@given(member_id=st.sampled_from(["H1", "h1", "a/b", "a b", ".."]))
@example(member_id="H1")
def test_revision_mismatch_does_not_acquire_member(member_id: str) -> None:
    async def exercise(implementation: Implementation) -> None:
        async with _workspaces(implementation) as workspaces:
            revision = workspaces.root.revision
            assert revision is not None
            # Workspaces.create_candidate promises no uniform failure type for
            # an unavailable revision. Both failures must leave ownership free.
            with pytest.raises((RuntimeError, CalledProcessError)):
                await workspaces.create_candidate("0" * 40, member_id=member_id)
            candidate = await workspaces.create_candidate(revision, member_id=member_id)
            assert candidate.revision == revision

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


@settings(max_examples=6)
@given(source=st.sampled_from(["root", "sibling"]), discard_source=st.booleans())
@example(source="root", discard_source=False)
@example(source="sibling", discard_source=False)
@example(source="sibling", discard_source=True)
def test_live_candidates_share_revision_availability(source: str, *, discard_source: bool) -> None:
    async def exercise(implementation: Implementation) -> None:
        async with _workspaces(implementation) as workspaces:
            candidate = await workspaces.create_candidate(member_id="older-member")
            sibling = (
                await workspaces.create_candidate(member_id="newer-member")
                if source == "sibling"
                else None
            )
            writer = workspaces.root if sibling is None else sibling
            # The recipient existed before this revision entered the shared
            # repository. Adoption is not required to make it available.
            writer.path.mkdir(parents=True, exist_ok=True)
            (writer.path / "candidate.py").write_text("VALUE = 4\n", encoding="utf-8")
            revision = await writer.snapshot("shared-revision")
            if sibling is not None and discard_source:
                await sibling.discard()
            await candidate.restore(revision)
            assert candidate.revision == revision
            await candidate.retain(revision, label="shared-revision")
            assert await candidate.try_restore(revision)
            assert candidate.revision == revision

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


@settings(max_examples=6)
@given(member_id=st.sampled_from(["H1", "h1", "a/b", "a b", ".."]))
@example(member_id="H1")
def test_concurrent_member_creation_has_one_owner(member_id: str) -> None:
    async def exercise(implementation: Implementation) -> None:
        async with _workspaces(implementation) as workspaces:
            first_revision = await workspaces.root.snapshot("first")
            (workspaces.root.path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
            second_revision = await workspaces.root.snapshot("second")
            requested_revisions = (first_revision, second_revision)
            results = await asyncio.gather(
                *(
                    workspaces.create_candidate(revision, member_id=member_id)
                    for revision in requested_revisions
                ),
                return_exceptions=True,
            )
            candidates = [result for result in results if not isinstance(result, BaseException)]
            failures = [result for result in results if isinstance(result, BaseException)]
            assert len(candidates) == len(failures) == 1
            assert isinstance(failures[0], RuntimeContractError)
            for result, revision in zip(results, requested_revisions, strict=True):
                if not isinstance(result, BaseException):
                    assert result.revision == revision
            candidate = candidates[0]
            owned_revision = candidate.revision
            owned_path = candidate.path
            with pytest.raises(RuntimeContractError, match="already has a live candidate"):
                await workspaces.create_candidate(second_revision, member_id=member_id)
            assert candidate.revision == owned_revision
            assert candidate.path == owned_path
            await candidate.discard()
            await candidate.discard()
            replacement = await workspaces.create_candidate(second_revision, member_id=member_id)
            assert replacement.path == owned_path
            assert replacement.revision == second_revision

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


def _ledger(workspaces: Workspaces) -> RevisionLedger:
    """Both implementations keep a revision ledger beside their workspaces."""
    return cast("RevisionLedger", workspaces)


def _dangling_revision(workspaces: Workspaces, serial: int) -> str:
    """A revision that exists in the repository but that nothing references."""
    if isinstance(workspaces, FakeWorkspaces):
        revision = f"dangling-{serial}"
        workspaces.add_dangling_revision(revision)
        return revision
    environment = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": os.environ["PATH"],
        "HOME": str(workspaces.root.path),
    }
    created = run_test_command(
        ["git", "commit-tree", "HEAD^{tree}", "-m", f"dangling {serial}"],
        cwd=workspaces.root.path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return created.stdout.strip()


@settings(max_examples=5, deadline=None)
@given(steps=st.lists(st.sampled_from(["root", "candidate", "dangling"]), min_size=1, max_size=6))
@example(steps=["dangling"])
def test_retention_is_reachability_not_presence(steps: list[str]) -> None:
    """Both implementations retain exactly what a snapshot or candidate kept, never a bare object.

    Exporting a patch succeeds for a dangling revision in both, so it cannot be the
    retention check; ``retains`` is, and an unknown revision is simply not retained.
    """

    async def exercise(implementation: Implementation) -> None:
        async with _workspaces(implementation) as workspaces:
            retained: set[str] = set()
            dangling: set[str] = set()
            for serial, step in enumerate(steps):
                (workspaces.root.path / "candidate.py").write_text(f"VALUE = {serial}\n")
                if step == "root":
                    retained.add(await workspaces.root.snapshot(f"root-{serial}"))
                elif step == "candidate":
                    candidate = await workspaces.create_candidate(member_id=f"m{serial}")
                    if implementation == "git":  # the fake candidate has no directory on disk
                        (candidate.path / "candidate.py").write_text(f"VALUE = {serial}0\n")
                    retained.add(await candidate.snapshot(f"candidate-{serial}"))
                    await candidate.discard()
                else:
                    dangling.add(_dangling_revision(workspaces, serial))
            for revision in retained:
                assert await _ledger(workspaces).retains(revision)
            for revision in dangling:
                assert not await _ledger(workspaces).retains(revision)
                await workspaces.export_patch(revision)
            assert not await _ledger(workspaces).retains("0" * 40)

    for implementation in _IMPLEMENTATIONS:
        asyncio.run(exercise(implementation))


def test_parent_verification_over_real_git_accepts_only_retained_commits() -> None:
    """The production owner, parser and Git ledger agree: a dangling commit never verifies."""

    async def exercise() -> None:
        async with _workspaces("git") as workspaces:
            owner = VerifyRevisionOwner(workspaces, _ledger(workspaces), commit_of)
            context = ExecutionContext(
                fence=HostFence(host_id=HostId(root="h"), epoch=1), now_at=5.0, payload_digest="d"
            )

            async def verified(commit: str) -> object:
                request = _VerifyRequest(parent=revision_ref(commit))
                return (await owner.execute(request, context))["verified"]

            retained = await workspaces.root.snapshot("retained")
            candidate = await workspaces.create_candidate(retained, member_id="parent")
            (candidate.path / "candidate.py").write_text("VALUE = 5\n", encoding="utf-8")
            held = await candidate.snapshot("held")
            await candidate.discard()
            assert await verified(retained) is True
            assert await verified(held) is True
            assert await verified(_dangling_revision(workspaces, 0)) is False
            assert await verified("0" * 40) is False

    asyncio.run(exercise())
