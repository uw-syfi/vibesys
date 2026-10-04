"""Workspace request executors over real Git-backed run workspaces."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.run_execution import run_execution_record

from vs_agent.api import NULL_AGENT_EVENT_SINK, NULL_SKILL_SELECTION
from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    AttemptId,
    AttemptRef,
    ContractError,
    DecisionId,
    DiscardWorkspace,
    EnsureWorkspace,
    HostFence,
    HostId,
    InvocationId,
    InvocationRef,
    ObservationStatus,
    RequestId,
    RestoreRevision,
    RetainedCandidate,
    RetainRevision,
    RevisionId,
    RevisionRef,
    RunId,
    RunInvocationCheckpointObserved,
    Scope,
    SessionId,
    SettlementId,
    SnapshotAndRetain,
    SnapshotAndRetainRun,
    TrustedBaseline,
    VerifyAdoption,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
)
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    RunEnvironmentRecord,
    run_git,
)
from vs_runtime.api.core import (
    DirectoryWorkspaceReceipts,
    ExecutionContext,
    ExecutionRecord,
    ExecutionResult,
    ExecutorRefusal,
    ReceiptPhase,
    RequestExecutors,
    RuntimeWorkspaceRequests,
    revision_ref,
)
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    RuntimeWorkspaces,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    create_run_control_channel,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink
from vs_sandbox.api import ProjectPathPolicy
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

    from vs_agent.api import AgentClientProtocol
    from vs_core.api import Request
    from vs_project.api import OrchestrationRunManifest
    from vs_runtime.api import AgentRole, OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration
    from vs_sandbox.api import Sandbox


@dataclass
class _Session:
    sandbox: Sandbox = field(default_factory=FakeSandbox)
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(
            paths=AgentPaths(), supports_parallel_candidate_evaluation=True
        )
    )

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def close(self) -> None:
        return None


class _Common(TypedDict):
    request_id: RequestId
    scope: Scope
    admission_id: DecisionId
    deadline_at: float


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _emit(text: str, writer: TextIO) -> None:
    writer.write(text + "\n")
    writer.flush()


@contextmanager
def _workspaces(tmp_path: Path) -> Iterator[RuntimeWorkspaces]:
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    request = ProjectRunRequest(
        project_root=root,
        run_id="workspace-requests",
        display_name="workspace requests",
        task_name=None,
        existing=False,
        framework_version="1.2.3",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )

    def unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        pytest.fail("fresh run resumed")

    def unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
        pytest.fail("workspace-only test opened an agent execution")

    def unexpected_client(**_kwargs: object) -> AgentClientProtocol:
        pytest.fail("workspace-only test opened an agent client")

    effects = ProjectRunEffects(
        git_events=NullGitTrackerEvents(), log_emit=_emit, on_log_ready=lambda _path: None
    )
    with open_project_run_resources(
        request, effects=effects, resolve_resume=unexpected_resume
    ) as project:
        environment = open_run_environment_resources(
            RunEnvironmentRequest(
                log_dir=project.logger.log_dir,
                workspace=root,
                ref_dir=None,
                backend=FakeComputeBackend(),
                agent_backend="stub",
                cli_provider="codex",
                run_id="workspace-requests",
                framework_root=tmp_path,
                project_path_policy=ProjectPathPolicy(),
                git_history_root=project.git.history_root,
            ),
            lambda _request: _Session(),
        )
        with closing(environment):
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
            )
            runtime = create_workspace_runtime(
                (),
                workspace_resources=factory,
                resolve_configuration=unexpected_execution,
                session_store=lambda: None,
                control=create_run_control_channel(FakeRunControlEventSink()),
                lifecycle_events=FakeAgentExecutionLifecycleSink(),
                agent_events=NULL_AGENT_EVENT_SINK,
                route_message=lambda message, _steering: message,
                blocking=BlockingOperations(),
                client_factory=unexpected_client,
            )
            try:
                yield runtime.workspaces
            finally:
                asyncio.run(runtime.workspaces.close())


def _rid(name: str) -> RequestId:
    return RequestId(root=name)


def _attempt(name: str = "a1", generation: int = 0) -> AttemptRef:
    return AttemptRef(attempt_id=AttemptId(root=name), generation=generation)


def _scope(attempt: AttemptRef) -> Scope:
    return Scope(owner=attempt.attempt_id, generation=attempt.generation)


def _common(attempt: AttemptRef, name: str) -> _Common:
    return {
        "request_id": _rid(name),
        "scope": _scope(attempt),
        "admission_id": DecisionId(root="admit"),
        "deadline_at": 100.0,
    }


def _context(request: Request, *, epoch: int = 1) -> ExecutionContext:
    digest = hashlib.sha256(repr(request).encode()).hexdigest()
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root="host"), epoch=epoch), now_at=1.0, payload_digest=digest
    )


def _git(path: Path, *args: str) -> str:
    result = run_git(list(args), cwd=path)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


def _executor(
    workspaces: RuntimeWorkspaces, directory: Path
) -> tuple[RuntimeWorkspaceRequests, DirectoryWorkspaceReceipts]:
    receipts = DirectoryWorkspaceReceipts(directory)
    return RuntimeWorkspaceRequests(workspaces, receipts), receipts


async def _run(
    executor: RuntimeWorkspaceRequests, request: Request, *, epoch: int = 1
) -> ExecutionResult:
    outcome = await executor.execute(request, _context(request, epoch=epoch))
    assert isinstance(outcome, ExecutionResult), outcome
    return outcome


def _ensure(
    workspaces: RuntimeWorkspaces,
    attempt: AttemptRef,
    name: str,
    mode: WorkspaceMode = WorkspaceMode.ISOLATED_CHILD,
) -> EnsureWorkspace:
    base = workspaces.root.revision
    assert base is not None
    return EnsureWorkspace(
        **_common(attempt, name),
        attempt=attempt,
        plan=WorkspacePlan(mode=mode, base=revision_ref(base)),
    )


def _worktrees(workspaces: RuntimeWorkspaces) -> int:
    return len(_git(workspaces.root.path, "worktree", "list").splitlines())


def _candidate_path(workspaces: RuntimeWorkspaces) -> Path:
    paths = [
        Path(line.split()[0])
        for line in _git(workspaces.root.path, "worktree", "list").splitlines()
    ]
    (candidate,) = [path for path in paths if path != workspaces.root.path.resolve()]
    return candidate


def _commits(workspaces: RuntimeWorkspaces) -> int:
    return int(_git(workspaces.root.path, "rev-list", "--all", "--count"))


def test_ensure_creates_one_candidate_and_replays_the_same_receipt(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        request = _ensure(workspaces, attempt, "ensure-1")
        before = _worktrees(workspaces)
        first = await _run(executor, request)
        assert first.observation.observation.status is ObservationStatus.SUCCEEDED
        assert first.observation.observation.accepted
        assert _worktrees(workspaces) == before + 1
        assert await _run(executor, request) == first
        # A new identity for the same attempt and plan reattaches, never duplicates.
        again = await _run(executor, _ensure(workspaces, attempt, "ensure-2"))
        assert (
            again.observation.observation.resource_id == first.observation.observation.resource_id
        )
        assert _worktrees(workspaces) == before + 1
        assert isinstance(first.owner_events[0], WorkspaceObserved)

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_ensure_replay_after_restart_reuses_the_durable_receipt(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        attempt = _attempt()
        request = _ensure(workspaces, attempt, "ensure-1")
        first_host, _ = _executor(workspaces, tmp_path / "receipts")
        first = await _run(first_host, request)
        before = _worktrees(workspaces)
        restarted, _ = _executor(workspaces, tmp_path / "receipts")
        assert await _run(restarted, request, epoch=2) == first
        assert _worktrees(workspaces) == before

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_ensure_rejects_foreign_base_and_conflicting_plan(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        foreign = EnsureWorkspace(
            **_common(attempt, "ensure-foreign"),
            attempt=attempt,
            plan=WorkspacePlan(mode=WorkspaceMode.ISOLATED_CHILD, base=revision_ref("f" * 40)),
        )
        before = _worktrees(workspaces)
        rejected = await _run(executor, foreign)
        assert rejected.observation.observation.status is ObservationStatus.REJECTED
        assert _worktrees(workspaces) == before
        await _run(executor, _ensure(workspaces, attempt, "ensure-ok"))
        conflicting = _ensure(workspaces, attempt, "ensure-root", WorkspaceMode.EXCLUSIVE_ROOT)
        result = await _run(executor, conflicting)
        assert result.observation.observation.status is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_snapshot_replay_makes_one_commit_and_binds_the_receipt(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        candidate_path = _candidate_path(workspaces)
        (candidate_path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        request = SnapshotAndRetain(
            **_common(attempt, "snap-1"),
            attempt=attempt,
            retention="wip",
        )
        first = await _run(executor, request)
        observed = first.observation
        assert observed.observation.status is ObservationStatus.SUCCEEDED
        assert observed.revision is not None
        assert observed.revision.digest == f"git-commit:{observed.revision.revision_id.root}"
        commits = _commits(workspaces)
        restarted, _ = _executor(workspaces, tmp_path / "receipts")
        assert await _run(restarted, request, epoch=2) == first
        assert await _run(executor, request) == first
        assert _commits(workspaces) == commits

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_interrupted_snapshot_returns_unknown_without_a_second_commit(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, receipts = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        request = SnapshotAndRetain(
            **_common(attempt, "snap-1"),
            attempt=attempt,
            retention="candidate",
        )
        # Host died after recording intent, before the side effect was acknowledged.
        receipts.save_execution(
            _rid("snap-1"),
            ExecutionRecord(
                payload_digest=_context(request).payload_digest, phase=ReceiptPhase.BEGUN
            ),
        )
        commits = _commits(workspaces)
        unknown = await _run(executor, request, epoch=2)
        assert unknown.observation.observation.status is ObservationStatus.UNKNOWN
        assert not unknown.observation.observation.terminal
        assert unknown.observation.revision is None
        assert _commits(workspaces) == commits

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_restore_is_exact_and_rejects_wrong_or_foreign_revisions(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        path = _candidate_path(workspaces)
        (path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        snap = await _run(
            executor,
            SnapshotAndRetain(
                **_common(attempt, "snap-1"),
                attempt=attempt,
                retention="wip",
            ),
        )
        assert snap.observation.revision is not None
        base = ensure.plan.base

        restore = RestoreRevision(
            **_common(attempt, "restore-base"),
            attempt=attempt,
            revision=base,
        )
        result = await _run(executor, restore)
        assert result.observation.observation.status is ObservationStatus.SUCCEEDED
        assert result.observation.revision == base
        assert (path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert await _run(executor, restore) == result

        wrong_digest = RevisionRef(revision_id=base.revision_id, digest="git-commit:other")
        foreign = revision_ref("0123456789abcdef0123456789abcdef01234567")
        malformed = RevisionRef(revision_id=RevisionId(root="not-a-commit"), digest="x")
        for index, revision in enumerate((wrong_digest, foreign, malformed)):
            rejected = await _run(
                executor,
                RestoreRevision(
                    **_common(attempt, f"restore-bad-{index}"),
                    attempt=attempt,
                    revision=revision,
                ),
            )
            assert rejected.observation.observation.status is ObservationStatus.REJECTED
            assert rejected.observation.revision is None
        assert (path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_retain_receipt_names_the_exact_revision_and_dedups_by_request(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        retain = RetainRevision(
            **_common(attempt, "retain-1"),
            attempt=attempt,
            revision=ensure.plan.base,
            retention="wip",
        )
        refs = _git(workspaces.root.path, "for-each-ref", "--count=1000")
        first = await _run(executor, retain)
        assert first.observation.revision == ensure.plan.base
        assert first.observation.observation.status is ObservationStatus.SUCCEEDED
        after_first = _git(workspaces.root.path, "for-each-ref", "--count=1000")
        assert after_first != refs
        assert await _run(executor, retain) == first
        assert _git(workspaces.root.path, "for-each-ref", "--count=1000") == after_first
        foreign = RetainRevision(
            **_common(attempt, "retain-foreign"),
            attempt=attempt,
            revision=revision_ref("f" * 40),
            retention="candidate",
        )
        rejected = await _run(executor, foreign)
        assert rejected.observation.observation.status is ObservationStatus.REJECTED
        assert _git(workspaces.root.path, "for-each-ref", "--count=1000") == after_first

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_run_snapshot_binds_invocation_request_and_retention(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        run_scope = Scope(owner=RunId(root="run-1"), generation=3)
        invocation = InvocationRef(
            session_id=SessionId(root="s1"), invocation_id=InvocationId(root="i1"), generation=3
        )
        request = SnapshotAndRetainRun(
            request_id=_rid("run-snap-1"),
            scope=run_scope,
            deadline_at=100.0,
            invocation=invocation,
            retention="candidate",
        )
        (workspaces.root.path / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
        first = await _run(executor, request)
        (event,) = first.owner_events
        assert isinstance(event, RunInvocationCheckpointObserved)
        assert event.invocation == invocation
        assert event.checkpoint_request == _rid("run-snap-1")
        assert event.revision == first.observation.revision
        assert event.revision is not None
        assert event.observation == first.observation.observation
        commits = _commits(workspaces)
        assert await _run(executor, request, epoch=2) == first
        assert _commits(workspaces) == commits
        assert _git(workspaces.root.path, "rev-parse", event.revision.revision_id.root)

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_discard_reports_release_and_is_idempotent(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        path = _candidate_path(workspaces)
        discard = DiscardWorkspace(**_common(attempt, "discard-1"), attempt=attempt)
        first = await _run(executor, discard)
        observation = first.observation.observation
        assert observation.status is ObservationStatus.SUCCEEDED
        assert observation.released
        assert observation.children_complete
        assert observation.children == ()
        assert not path.exists()
        assert await _run(executor, discard) == first
        # A different request for the same released lease re-inspects, not recreates.
        later = await _run(
            executor,
            DiscardWorkspace(**_common(attempt, "discard-2"), attempt=attempt),
        )
        assert later.observation.observation.released
        assert later.observation.observation.resource_id == observation.resource_id
        restored = await _run(
            executor,
            RestoreRevision(
                **_common(attempt, "restore-late"),
                attempt=attempt,
                revision=revision_ref(workspaces.root.revision or ""),
            ),
        )
        assert restored.observation.observation.status is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_discard_of_the_exclusive_root_is_rejected(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = await _run(
            executor, _ensure(workspaces, attempt, "ensure-root", WorkspaceMode.EXCLUSIVE_ROOT)
        )
        assert ensure.observation.observation.status is ObservationStatus.SUCCEEDED
        result = await _run(
            executor,
            DiscardWorkspace(**_common(attempt, "discard-root"), attempt=attempt),
        )
        assert result.observation.observation.status is ObservationStatus.REJECTED
        assert not result.observation.observation.released

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_identity_conflicts_stale_hosts_and_unowned_requests(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        request = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, request)
        conflicting = DiscardWorkspace(**_common(attempt, "ensure-1"), attempt=attempt)
        outcome = await executor.execute(conflicting, _context(conflicting))
        assert isinstance(outcome, ExecutorRefusal)
        with pytest.raises(ContractError):
            await executor.execute(request, _context(request, epoch=0))
        await _run(executor, request, epoch=5)
        with pytest.raises(ContractError):
            await executor.execute(request, _context(request, epoch=4))

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_adoption_applies_inspects_and_verifies_the_selected_revision(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, receipts = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        (_candidate_path(workspaces) / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        snap = await _run(
            executor,
            SnapshotAndRetain(
                **_common(attempt, "snap-1"),
                attempt=attempt,
                retention="candidate",
            ),
        )
        winner = snap.observation.revision
        assert winner is not None
        run_scope = Scope(owner=RunId(root="run-1"), generation=0)
        selection = RetainedCandidate(settlement_id=SettlementId(root="settle"), revision=winner)

        def verify(name: str) -> VerifyAdoption:
            return VerifyAdoption(
                request_id=_rid(name), scope=run_scope, deadline_at=100.0, selection=selection
            )

        before = await _run(executor, verify("verify-0"))
        assert before.observation.observation.status is ObservationStatus.UNKNOWN
        assert before.observation.revision is None
        adopt = AdoptRevision(
            request_id=_rid("adopt-1"), scope=run_scope, deadline_at=100.0, selection=selection
        )
        applied = await _run(executor, adopt)
        assert applied.observation.observation.status is ObservationStatus.SUCCEEDED
        assert applied.observation.revision == winner
        assert isinstance(applied.owner_events[0], AdoptionObserved)
        root_file = workspaces.root.path / "candidate.py"
        assert root_file.read_text(encoding="utf-8") == "VALUE = 2\n"
        assert await _run(executor, adopt, epoch=2) == applied
        proof = await _run(executor, verify("verify-1"), epoch=2)
        assert proof.observation.observation.status is ObservationStatus.SUCCEEDED
        assert proof.observation.revision == winner
        # Interrupted after intent: the root already proves the content, so no replay.
        again = AdoptRevision(
            request_id=_rid("adopt-2"), scope=run_scope, deadline_at=100.0, selection=selection
        )
        receipts.save_execution(
            _rid("adopt-2"),
            ExecutionRecord(
                payload_digest=_context(again).payload_digest, phase=ReceiptPhase.BEGUN
            ),
        )
        resumed = await _run(executor, again, epoch=3)
        assert resumed.observation.observation.status is ObservationStatus.SUCCEEDED
        # Drift after adoption is never reported as verified.
        root_file.write_text("VALUE = 3\n", encoding="utf-8")
        drifted = await _run(executor, verify("verify-2"), epoch=3)
        assert drifted.observation.observation.status is ObservationStatus.UNKNOWN

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_adoption_rejects_foreign_revisions_and_non_baseline_baselines(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        run_scope = Scope(owner=RunId(root="run-1"), generation=0)
        baseline = workspaces.root.trusted_input_baseline
        assert baseline is not None
        good = await _run(
            executor,
            AdoptRevision(
                request_id=_rid("adopt-baseline"),
                scope=run_scope,
                deadline_at=100.0,
                selection=TrustedBaseline(revision=revision_ref(baseline)),
            ),
        )
        assert good.observation.observation.status is ObservationStatus.SUCCEEDED
        for index, selection in enumerate(
            (
                RetainedCandidate(
                    settlement_id=SettlementId(root="s"), revision=revision_ref("a" * 40)
                ),
                TrustedBaseline(revision=revision_ref("b" * 40)),
            )
        ):
            rejected = await _run(
                executor,
                AdoptRevision(
                    request_id=_rid(f"adopt-bad-{index}"),
                    scope=run_scope,
                    deadline_at=100.0,
                    selection=selection,
                ),
            )
            assert rejected.observation.observation.status is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_dispatch_table_routes_every_workspace_role_request_to_the_executor(
    tmp_path: Path,
) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        executors = RequestExecutors(workspaces=executor)
        request = _ensure(workspaces, _attempt(), "ensure-1")
        outcome = await executors.dispatch(request, _context(request))
        assert isinstance(outcome, ExecutionResult)
        assert outcome.observation.observation.status is ObservationStatus.SUCCEEDED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


_COMMIT = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(commit=_COMMIT, digest=st.text(min_size=1, max_size=20))
def test_no_forged_revision_changes_a_workspace(
    tmp_path_factory: pytest.TempPathFactory, commit: str, digest: str
) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces, tmp_path / "receipts")
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        known = ensure.plan.base
        path = _candidate_path(workspaces)
        before = (path / "candidate.py").read_text(encoding="utf-8")
        commits = _commits(workspaces)
        forged = (
            RevisionRef(revision_id=RevisionId(root=commit), digest=f"git-commit:{commit}"),
            RevisionRef(revision_id=known.revision_id, digest=digest),
        )
        for index, revision in enumerate(forged):
            if revision == known:
                continue
            for number, request in enumerate(
                (
                    RestoreRevision(
                        **_common(attempt, f"r-{index}"),
                        attempt=attempt,
                        revision=revision,
                    ),
                    RetainRevision(
                        **_common(attempt, f"t-{index}"),
                        attempt=attempt,
                        revision=revision,
                        retention="wip",
                    ),
                )
            ):
                del number
                result = await _run(executor, request)
                assert result.observation.observation.status is ObservationStatus.REJECTED
                assert result.observation.revision is None
        assert (path / "candidate.py").read_text(encoding="utf-8") == before
        assert _commits(workspaces) == commits

    tmp_path = tmp_path_factory.mktemp("forged")
    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))
