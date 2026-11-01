"""Workspace request executors over real Git-backed run workspaces."""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import shutil
import weakref
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, TypedDict, cast

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.support.real_git import REAL_GIT
from tests.support.session_world import RunningRunInvocations, SettledRunInvocations

from vs_agent.api import NULL_SKILL_SELECTION
from vs_core.api import (
    AdoptionObserved,
    AdoptRevision,
    AttemptId,
    AttemptRef,
    CloseAttemptScope,
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
    Until,
    VerifyAdoption,
    WorkspaceMode,
    WorkspaceObserved,
    WorkspacePlan,
)
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    git_repositories,
)
from vs_project.api.testing import run_execution_record
from vs_runtime.api import RuntimeContractError
from vs_runtime.api.core import (
    ExecutionContext,
    ExecutionResult,
    ReceiptEvidenceLedger,
    ReceiptStore,
    RequestExecutors,
    RuntimeWorkspaceRequests,
    revision_ref,
)
from vs_runtime.api.executor_contexts import RevocableLease
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    RuntimeWorkspaces,
    TrustedEvaluationPlan,
    WorkspaceHolds,
    WorkspaceResource,
    WorkspaceResourceFactory,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.observation_contracts import assert_core_accepts
from vs_runtime.api.testing import MemoryValidationPasses, idle_environment_system
from vs_sandbox.api import CaptureExecutionIdentity, CaptureExecutionKind, ProjectPathPolicy
from vs_sandbox.api.testing import FakeCommandRunner, FakeComputeBackend
from vs_sim.api.testing import (
    InlineBlockingRunner,
    ManualClock,
    Sim,
    SimThreads,
    examples,
    swept,
    wait_or_fail,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import TextIO

    from pydantic import BaseModel

    from vs_agent.api import AgentClientProtocol
    from vs_core.api import Request
    from vs_project.api import OrchestrationRunManifest, StateNamespace
    from vs_runtime.api import AgentRole, OrchestrationResumeDecision
    from vs_runtime.api.core import ExecutionLease
    from vs_runtime.api.infrastructure import AgentExecutionConfiguration, WorkspaceResourceProvider
    from vs_sandbox.api import CommandRunner
    from vs_sim.api import BlockingRunner, Event
    from vs_sim.api.testing import Gate

# Each example builds real workspaces. A pull request draws a sample; the nightly depth
# (`--depth=nightly`) searches deeper.


@dataclass
class _Session:
    sandbox: CommandRunner = field(default_factory=FakeCommandRunner)
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(
            paths=AgentPaths(), workspace_git_writable=True, cli_sandboxed=True
        )
    )
    capture_execution: CaptureExecutionIdentity | None = None

    @property
    def capture_commands(self) -> CommandRunner:
        return self.sandbox

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


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _emit(text: str, writer: TextIO) -> None:
    writer.write(text + "\n")
    writer.flush()


@dataclass
class _Env:
    """One run's disk state and the hosts (RuntimeWorkspaces) that operate on it."""

    project: Project
    run_id: str
    factory: WorkspaceResourceProvider
    blocking: BlockingRunner
    threads: SimThreads
    hosts: list[RuntimeWorkspaces] = field(default_factory=list)

    def start_host(self) -> RuntimeWorkspaces:
        """Start another host over the same disk state, as after a process restart."""
        runtime = create_workspace_runtime(
            workspace_resources=self.factory,
            blocking=BlockingOperations(runner=self.blocking, threads=self.threads),
            validation_passes=MemoryValidationPasses(),
        )
        self.hosts.append(runtime.workspaces)
        _ENVS[runtime.workspaces] = self
        return runtime.workspaces

    def store(self) -> ReceiptStore:
        """A store over the run's durable receipts (a new one models a restart)."""
        return ReceiptStore(
            self.project.state.local_namespace(self.run_id, "receipts"), threads=SimThreads()
        )


_ENVS: weakref.WeakKeyDictionary[RuntimeWorkspaces, _Env] = weakref.WeakKeyDictionary()


def _unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
    pytest.fail("workspace-only test opened an agent execution")


def _unexpected_client(**_kwargs: object) -> AgentClientProtocol:
    pytest.fail("workspace-only test opened an agent client")


@contextmanager
def _env(
    tmp_path: Path,
    wrap: Callable[[WorkspaceResourceFactory], WorkspaceResourceProvider] = lambda f: f,
    blocking: BlockingRunner | None = None,
    *,
    recoverable_capture: bool = False,
) -> Iterator[_Env]:
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
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )

    def unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        pytest.fail("fresh run resumed")

    effects = ProjectRunEffects(
        git_events=NullGitTrackerEvents(),
        log_emit=_emit,
        on_log_ready=lambda _path: None,
        clock=ManualClock(),
        git_repository=git_repositories(REAL_GIT),
    )
    with open_project_run_resources(
        request, effects=effects, resolve_resume=unexpected_resume
    ) as project:
        environment = open_run_environment_resources(
            RunEnvironmentRequest(
                log_dir=project.logger.log_dir,
                workspace=root,
                ref_dir=None,
                backend=FakeComputeBackend(threads=SimThreads()),
                agent_backend="stub",
                cli_provider="codex",
                run_id="workspace-requests",
                framework_root=tmp_path,
                project_path_policy=ProjectPathPolicy(),
                git_history_root=project.git.history_root,
                clock=ManualClock(),
                system=idle_environment_system(),
            ),
            lambda session_request: _Session(
                capture_execution=(
                    CaptureExecutionIdentity(
                        kind=CaptureExecutionKind.SLURM_GPU,
                        execution_id=(
                            "vibesys-test-capture-"
                            + hashlib.sha256(
                                str(session_request.workspace.resolve()).encode()
                            ).hexdigest()[:8]
                        ),
                    )
                    if recoverable_capture
                    else None
                )
            ),
            clock=ManualClock(),
        )
        with closing(environment):
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(unread_paths=("NOTES.md",)),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
                threads=SimThreads(),
                blocking=InlineBlockingRunner(),
            )
            env = _Env(
                project.project,
                "workspace-requests",
                wrap(factory),
                blocking=blocking or InlineBlockingRunner(),
                threads=SimThreads(),
            )
            env.start_host()
            try:
                yield env
            finally:
                for host in reversed(env.hosts):
                    asyncio.run(host.close())


@contextmanager
def _workspaces(tmp_path: Path) -> Iterator[RuntimeWorkspaces]:
    with _env(tmp_path) as env:
        yield env.hosts[0]


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
    }


def _context(
    request: Request,
    *,
    epoch: int = 1,
    host: str = "host",
    lease: ExecutionLease | None = None,
    repeat: bool = False,
) -> ExecutionContext:
    digest = hashlib.sha256(repr(request).encode()).hexdigest()
    return ExecutionContext(
        fence=HostFence(host_id=HostId(root=host), epoch=epoch),
        now_at=1.0,
        payload_digest=digest,
        lease=lease or RevocableLease(),
        until=Until(at=100.0),
        repeat=repeat,
    )


def _git(path: Path, *args: str) -> str:
    result = REAL_GIT.run(list(args), cwd=path)
    assert result.returncode == 0, result.stderr
    return result.stdout.decode().strip()


def _executor(
    workspaces: RuntimeWorkspaces, store: ReceiptStore | None = None
) -> tuple[RuntimeWorkspaceRequests, ReceiptStore]:
    """A host's executor over the run's durable receipts (a new one models a restart)."""
    chosen = store or _ENVS[workspaces].store()
    executor = RuntimeWorkspaceRequests(
        workspaces,
        chosen,
        SettledRunInvocations(),
        blocking=InlineBlockingRunner(),
        evidence=ReceiptEvidenceLedger(chosen),
    )
    return executor, chosen


async def _run(
    executor: RuntimeWorkspaceRequests,
    request: Request,
    *,
    epoch: int = 1,
    host: str = "host",
    repeat: bool = False,
) -> ExecutionResult:
    """Run *request*; with *repeat*, as an attempt after one that may have run."""
    outcome = await executor.execute(
        request, _context(request, epoch=epoch, host=host, repeat=repeat)
    )
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
        executor, _ = _executor(workspaces)
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
        first_host, _ = _executor(workspaces)
        first = await _run(first_host, request)
        before = _worktrees(workspaces)
        restarted, _ = _executor(workspaces)
        assert await _run(restarted, request, epoch=2) == first
        assert _worktrees(workspaces) == before

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_ensure_rejects_foreign_base_and_conflicting_plan(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
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
        executor, _ = _executor(workspaces)
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
        assert observed.observation.released  # a closure retention (no invocation)
        assert observed.revision is not None
        assert observed.revision.digest == f"git-commit:{observed.revision.revision_id.root}"
        commits = _commits(workspaces)
        restarted, _ = _executor(workspaces)
        assert await _run(restarted, request, epoch=2) == first
        assert await _run(executor, request, epoch=2) == first
        assert _commits(workspaces) == commits

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


@pytest.mark.parametrize("kind", ["directory", "file"])
def test_a_snapshot_of_a_workspace_holding_a_nested_repository_fails_and_names_it(
    tmp_path: Path, kind: str
) -> None:
    """Host Git never reads a nested repository the agent made; the snapshot says why."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        candidate_path = _candidate_path(workspaces)
        commits = _commits(workspaces)
        nested = candidate_path / "vendor" / "lib" / ".git"
        nested.parent.mkdir(parents=True)
        if kind == "directory":
            nested.mkdir()
        else:
            nested.write_text("gitdir: /elsewhere\n", encoding="utf-8")
        request = SnapshotAndRetain(**_common(attempt, "snap-1"), attempt=attempt, retention="wip")

        refused = await _run(executor, request)

        observation = refused.observation.observation
        assert observation.status is ObservationStatus.FAILED
        assert observation.terminal
        assert "vendor/lib/.git" in observation.diagnostic
        assert refused.observation.revision is None
        assert _commits(workspaces) == commits

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


class _FailingStore(ReceiptStore):
    """A real store whose chosen writes fail, as when a host dies at that point."""

    def __init__(self, namespace: StateNamespace) -> None:
        super().__init__(namespace, SimThreads())
        self.fail_release_mark = False
        self.fail_bind = False

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        if self.fail_release_mark and family == "workspace-released":
            message = "host died before recording the release"
            raise OSError(message)
        super().replace(family, part, key, receipt)

    def modify[ReceiptT: BaseModel, ResultT](
        self,
        family: str,
        part: str,
        key: str,
        model: type[ReceiptT],
        decide: Callable[[ReceiptT | None], tuple[ReceiptT | None, ResultT]],
    ) -> ResultT:
        if self.fail_bind and family == "workspace-bindings":
            message = "host died before binding the workspace"
            raise OSError(message)
        return super().modify(family, part, key, model, decide)


def _failing(env: _Env) -> _FailingStore:
    return _FailingStore(env.project.state.local_namespace(env.run_id, "receipts"))


def test_a_write_turns_snapshot_keeps_its_edits_and_a_candidate_can_retain_it(
    tmp_path: Path,
) -> None:
    """The revision a terminal write turn made is retained once, then settled as a candidate."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
        candidate_path = _candidate_path(workspaces)
        ref = InvocationRef(
            session_id=SessionId(root="implementer"),
            invocation_id=InvocationId(root="turn-1"),
            generation=0,
        )
        unchanged = await _run(
            executor,
            SnapshotAndRetain(
                **_common(attempt, "snap-0"), attempt=attempt, retention="wip", invocation=ref
            ),
        )
        commits = _commits(workspaces)
        (candidate_path / "candidate.py").write_text("VALUE = 7\n", encoding="utf-8")
        edited = await _run(
            executor,
            SnapshotAndRetain(
                **_common(attempt, "snap-1"), attempt=attempt, retention="wip", invocation=ref
            ),
        )
        assert _status(edited) is ObservationStatus.SUCCEEDED
        assert not edited.observation.observation.released  # one checkpoint among many
        assert edited.observation.revision is not None
        assert edited.observation.revision != unchanged.observation.revision
        assert _commits(workspaces) == commits + 1
        # An unchanged tree retains the existing revision, not a second commit.
        again = await _run(
            executor,
            SnapshotAndRetain(
                **_common(attempt, "snap-2"), attempt=attempt, retention="wip", invocation=ref
            ),
        )
        assert again.observation.revision == edited.observation.revision
        assert _commits(workspaces) == commits + 1
        # Closure upgrades that same checkpoint to a candidate by its revision.
        kept = await _run(
            executor,
            RetainRevision(
                **_common(attempt, "retain-1"),
                attempt=attempt,
                revision=edited.observation.revision,
                retention="candidate",
            ),
        )
        assert _status(kept) is ObservationStatus.SUCCEEDED
        assert kept.observation.revision == edited.observation.revision

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_interrupted_snapshot_is_recovered_by_its_label_without_a_second_commit(
    tmp_path: Path,
) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            crashing = _failing(env)
            executor, _ = _executor(workspaces, crashing)
            attempt = _attempt()
            await _run(executor, _ensure(workspaces, attempt, "ensure-1"))
            (_candidate_path(workspaces) / "candidate.py").write_text("VALUE = 5\n")
            request = SnapshotAndRetain(
                **_common(attempt, "snap-1"), attempt=attempt, retention="candidate"
            )
            taken = await _run(executor, request)  # the host dies before its result is recorded
            commits = _commits(workspaces)
            restarted, _ = _executor(workspaces)
            recovered = await _run(restarted, request, epoch=2, repeat=True)
            assert recovered.observation.observation.status is ObservationStatus.SUCCEEDED
            assert recovered.observation.revision == taken.observation.revision
            assert _commits(workspaces) == commits
            again = await _run(restarted, request, epoch=2, repeat=True)
            assert again.observation.revision == recovered.observation.revision
            assert _commits(workspaces) == commits

        asyncio.run(exercise())


def test_restore_is_exact_and_rejects_wrong_or_foreign_revisions(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
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
        executor, _ = _executor(workspaces)
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
        # The closure waits for its retention to be released before it discards the workspace.
        assert first.observation.observation.released
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
        executor, _ = _executor(workspaces)
        run_scope = Scope(owner=RunId(root="run-1"), generation=3)
        invocation = InvocationRef(
            session_id=SessionId(root="s1"), invocation_id=InvocationId(root="i1"), generation=3
        )
        request = SnapshotAndRetainRun(
            request_id=_rid("run-snap-1"),
            scope=run_scope,
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


def test_run_snapshot_waits_for_proof_that_the_writer_ended(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        store = _ENVS[workspaces].store()
        running = RuntimeWorkspaceRequests(
            workspaces,
            store,
            RunningRunInvocations(),
            blocking=InlineBlockingRunner(),
            evidence=ReceiptEvidenceLedger(store),
        )
        request = SnapshotAndRetainRun(
            request_id=_rid("run-snap-wait"),
            scope=Scope(owner=RunId(root="run-1"), generation=3),
            invocation=InvocationRef(
                session_id=SessionId(root="s1"),
                invocation_id=InvocationId(root="i1"),
                generation=3,
            ),
            retention="candidate",
        )
        (workspaces.root.path / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
        commits = _commits(workspaces)
        waiting = await _run(running, request)
        observed = waiting.observation.observation
        assert observed.status is ObservationStatus.UNKNOWN
        assert not observed.terminal
        assert waiting.observation.revision is None
        assert _commits(workspaces) == commits
        settled = RuntimeWorkspaceRequests(
            workspaces,
            store,
            SettledRunInvocations(),
            blocking=InlineBlockingRunner(),
            evidence=ReceiptEvidenceLedger(store),
        )
        done = await _run(settled, request)
        assert done.observation.observation.status is ObservationStatus.SUCCEEDED
        assert_core_accepts([waiting, done], expect_retry=True)

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_discard_reports_release_and_is_idempotent(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
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


def test_discard_of_the_exclusive_root_releases_the_hold_and_keeps_the_root(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        ensure = await _run(
            executor, _ensure(workspaces, attempt, "ensure-root", WorkspaceMode.EXCLUSIVE_ROOT)
        )
        assert ensure.observation.observation.status is ObservationStatus.SUCCEEDED
        result = await _run(
            executor,
            DiscardWorkspace(**_common(attempt, "discard-root"), attempt=attempt),
        )
        assert result.observation.observation.status is ObservationStatus.SUCCEEDED
        assert result.observation.observation.released
        assert workspaces.root.path.is_dir()

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_adoption_publishes_and_legacy_verification_reads_publication(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
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
            return VerifyAdoption(request_id=_rid(name), scope=run_scope, selection=selection)

        before = await _run(executor, verify("verify-0"))
        assert before.observation.observation.status is ObservationStatus.SUCCEEDED
        assert before.observation.observation.terminal
        assert before.observation.revision == winner
        assert (workspaces.root.path / "candidate.py").read_text() == "VALUE = 1\n"
        adopt = AdoptRevision(request_id=_rid("adopt-1"), scope=run_scope, selection=selection)
        applied = await _run(executor, adopt)
        assert applied.observation.observation.status is ObservationStatus.SUCCEEDED
        assert applied.observation.revision == winner
        assert isinstance(applied.owner_events[0], AdoptionObserved)
        root_file = workspaces.root.path / "candidate.py"
        assert root_file.read_text(encoding="utf-8") == "VALUE = 2\n"
        replayed = await _run(executor, adopt, epoch=2, repeat=True)
        assert replayed.observation.observation.status is ObservationStatus.SUCCEEDED
        assert replayed.observation.revision == applied.observation.revision
        before_head = workspaces.root.revision
        same = AdoptRevision(request_id=_rid("adopt-same"), scope=run_scope, selection=selection)
        checked = await _run(executor, same, epoch=2)
        assert checked.observation.observation.status is ObservationStatus.SUCCEEDED
        assert workspaces.root.revision == before_head
        proof = await _run(executor, verify("verify-1"), epoch=2)
        assert proof.observation.observation.status is ObservationStatus.SUCCEEDED
        assert proof.observation.revision == winner
        # Interrupted after publication: the source receipt proves the same commit,
        # so the retry never restores the root or creates a second publication.
        again = AdoptRevision(request_id=_rid("adopt-2"), scope=run_scope, selection=selection)
        published = await _run(
            executor, again, epoch=3
        )  # the host dies before it records the result
        resumed = await _run(executor, again, epoch=3, repeat=True)
        assert resumed.observation.observation.status is ObservationStatus.SUCCEEDED
        assert resumed.observation.revision == published.observation.revision
        # Checkout drift does not invalidate a durable publication.
        root_file.write_text("VALUE = 3\n", encoding="utf-8")
        drifted = await _run(executor, verify("verify-2"), epoch=3)
        assert drifted.observation.observation.status is ObservationStatus.SUCCEEDED
        assert drifted.observation.observation.terminal
        assert root_file.read_text() == "VALUE = 3\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_adoption_rejects_foreign_revisions_and_non_baseline_baselines(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
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
        executor, _ = _executor(workspaces)
        executors = RequestExecutors(workspaces=executor)
        request = _ensure(workspaces, _attempt(), "ensure-1")
        outcome = await executors.dispatch(request, _context(request))
        assert isinstance(outcome, ExecutionResult)
        assert outcome.observation.observation.status is ObservationStatus.SUCCEEDED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


_COMMIT = st.text(alphabet="0123456789abcdef", min_size=40, max_size=40)


# Each example opens one workspace and tries several forgeries against it, because opening
# the workspace costs far more than a rejected request.
@settings(
    max_examples=examples(pr=2, nightly=6),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    commits=st.lists(_COMMIT, min_size=1, max_size=3),
    digests=st.lists(st.text(min_size=1, max_size=20), min_size=1, max_size=3),
)
def test_no_forged_revision_changes_a_workspace(
    tmp_path_factory: pytest.TempPathFactory, commits: list[str], digests: list[str]
) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        ensure = _ensure(workspaces, attempt, "ensure-1")
        await _run(executor, ensure)
        known = ensure.plan.base
        path = _candidate_path(workspaces)
        before = (path / "candidate.py").read_text(encoding="utf-8")
        known_commits = _commits(workspaces)
        forged = (
            *(
                RevisionRef(revision_id=RevisionId(root=commit), digest=f"git-commit:{commit}")
                for commit in commits
            ),
            *(RevisionRef(revision_id=known.revision_id, digest=digest) for digest in digests),
        )
        for index, revision in enumerate(forged):
            if revision == known:
                continue
            for request in (
                RestoreRevision(
                    **_common(attempt, f"r-{index}"), attempt=attempt, revision=revision
                ),
                RetainRevision(
                    **_common(attempt, f"t-{index}"),
                    attempt=attempt,
                    revision=revision,
                    retention="wip",
                ),
            ):
                result = await _run(executor, request)
                assert result.observation.observation.status is ObservationStatus.REJECTED
                assert result.observation.revision is None
        assert (path / "candidate.py").read_text(encoding="utf-8") == before
        assert _commits(workspaces) == known_commits

    tmp_path = tmp_path_factory.mktemp("forged")
    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


# Regression tests for the review of PR 1303 ------------------------------------


def _worktree_paths(workspaces: RuntimeWorkspaces) -> set[Path]:
    paths = {
        Path(line.split()[0])
        for line in _git(workspaces.root.path, "worktree", "list").splitlines()
    }
    return paths - {workspaces.root.path.resolve()}


async def _ensure_at(
    executor: RuntimeWorkspaceRequests,
    workspaces: RuntimeWorkspaces,
    attempt: AttemptRef,
    name: str,
) -> Path:
    """Ensure an attempt's workspace and return its path."""
    before = _worktree_paths(workspaces)
    result = await _run(executor, _ensure(workspaces, attempt, name))
    assert result.observation.observation.status is ObservationStatus.SUCCEEDED
    (path,) = _worktree_paths(workspaces) - before
    return path


async def _snapshot_of(
    executor: RuntimeWorkspaceRequests,
    attempt: AttemptRef,
    name: str,
    retention: Literal["wip", "candidate"] = "candidate",
    *,
    epoch: int = 1,
) -> RevisionRef:
    result = await _run(
        executor,
        SnapshotAndRetain(**_common(attempt, name), attempt=attempt, retention=retention),
        epoch=epoch,
    )
    assert result.observation.revision is not None
    return result.observation.revision


def _adopt[Kind: (AdoptRevision, VerifyAdoption)](
    name: str, revision: RevisionRef, kind: type[Kind]
) -> Kind:
    return kind(
        request_id=_rid(name),
        scope=Scope(owner=RunId(root="run-1"), generation=0),
        selection=RetainedCandidate(settlement_id=SettlementId(root="s"), revision=revision),
    )


def _status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


def _visible_files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in root.rglob("*")
        if path.is_file() and not (set(path.relative_to(root).parts) & {".git", ".vibesys"})
    }


def test_f1_added_files_are_applied_by_adopt_and_restore(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        a = _attempt("a")
        path = await _ensure_at(executor, workspaces, a, "ea")
        (path / "new.py").write_text("X = 1\n", encoding="utf-8")
        revision = await _snapshot_of(executor, a, "sa")
        (path / "new.py").unlink()
        restored = await _run(
            executor,
            RestoreRevision(**_common(a, "ra"), attempt=a, revision=revision),
        )
        assert _status(restored) is ObservationStatus.SUCCEEDED
        assert (path / "new.py").read_text(encoding="utf-8") == "X = 1\n"
        adopted = await _run(executor, _adopt("ad", revision, AdoptRevision))
        assert _status(adopted) is ObservationStatus.SUCCEEDED
        assert (workspaces.root.path / "new.py").read_text(encoding="utf-8") == "X = 1\n"
        verified = await _run(executor, _adopt("v", revision, VerifyAdoption))
        assert _status(verified) is ObservationStatus.SUCCEEDED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


_TREE = st.dictionaries(
    st.sampled_from(("candidate.py", "a.txt", "d/b.txt", "d/e/c.txt")),
    st.sampled_from(("1", "2")),
)


@settings(
    max_examples=examples(pr=2, nightly=8),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(tree=_TREE)
def test_f1_adopt_reproduces_any_candidate_tree_in_the_root(
    tmp_path_factory: pytest.TempPathFactory, tree: dict[str, str]
) -> None:
    """Added, deleted, modified and nested paths all reach the root, or adopt is not SUCCEEDED."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        path = await _ensure_at(executor, workspaces, attempt, "e")
        for child in path.iterdir():
            if child.name not in {".git", ".vibesys"}:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        for name, text in tree.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text, encoding="utf-8")
        revision = await _snapshot_of(executor, attempt, "s")
        adopted = await _run(executor, _adopt("ad", revision, AdoptRevision))
        assert _status(adopted) is ObservationStatus.SUCCEEDED
        assert _visible_files(workspaces.root.path) == tree
        assert _status(await _run(executor, _adopt("v", revision, VerifyAdoption))) is (
            ObservationStatus.SUCCEEDED
        )

    with _workspaces(tmp_path_factory.mktemp("tree")) as workspaces:
        asyncio.run(exercise(workspaces))


def _write_ignored_user_files(root: Path) -> dict[str, bytes]:
    """Create ignored files a user keeps in a checkout and return their exact bytes."""
    files = {
        ".venv/pyvenv.cfg": b"home = /usr/bin\n",
        ".venv/lib/site.py": b"print('site')\n",
        "cache/model.bin": bytes(range(256)),
        "local.env": b"TOKEN=secret\n",
    }
    for name, content in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(content)
    return files


def _ignored_snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for top in (".venv", "cache", "local.env")
        if (root / top).exists()
        for path in ([root / top] if (root / top).is_file() else (root / top).rglob("*"))
        if path.is_file()
    }


def _commit_ignore_rules(root: Path) -> None:
    (root / ".gitignore").write_text(".venv/\ncache/\nlocal.env\nstale.out\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-m", "ignore")


def test_root_adopt_and_restore_never_delete_ignored_user_files(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        root = workspaces.root
        _commit_ignore_rules(root.path)
        attempt = _attempt()
        path = await _ensure_at(executor, workspaces, attempt, "e")
        (path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        revision = await _snapshot_of(executor, attempt, "s")
        (root.path / "untracked.txt").write_text("leftover", encoding="utf-8")
        user_files = _write_ignored_user_files(root.path)

        adopted = await _run(executor, _adopt("ad", revision, AdoptRevision))
        assert _status(adopted) is ObservationStatus.SUCCEEDED
        assert (root.path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"
        assert (root.path / "untracked.txt").read_text() == "leftover"
        assert _ignored_snapshot(root.path) == user_files

        (root.path / "candidate.py").write_text("VALUE = 3\n", encoding="utf-8")
        commit = revision.digest.removeprefix("git-commit:")
        await root.restore(cast("str", root.revision))
        await root.restore(commit)
        assert (root.path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"
        assert _ignored_snapshot(root.path) == user_files

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_legacy_verification_preserves_all_checkout_content(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        root = workspaces.root.path
        _commit_ignore_rules(root)
        attempt = _attempt()
        path = await _ensure_at(executor, workspaces, attempt, "e")
        (path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        revision = await _snapshot_of(executor, attempt, "s")
        await _run(executor, _adopt("ad", revision, AdoptRevision))
        for index, content in enumerate((b"one", b"two", b"")):
            (root / "stale.out").write_bytes(content)
            (root / "cache").mkdir(exist_ok=True)
            (root / "cache" / "x").write_bytes(content)
            verified = await _run(executor, _adopt(f"v{index}", revision, VerifyAdoption))
            assert _status(verified) is ObservationStatus.SUCCEEDED
        (root / "candidate.py").write_text("VALUE = 9\n", encoding="utf-8")
        drifted = await _run(executor, _adopt("vd", revision, VerifyAdoption))
        assert _status(drifted) is ObservationStatus.SUCCEEDED
        assert drifted.observation.observation.terminal
        assert (root / "candidate.py").read_text() == "VALUE = 9\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_candidate_worktree_restore_cleans_ignored_leftovers_exactly(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        _commit_ignore_rules(workspaces.root.path)
        attempt = _attempt()
        path = await _ensure_at(executor, workspaces, attempt, "e")
        (path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        revision = await _snapshot_of(executor, attempt, "s")
        _write_ignored_user_files(path)
        (path / "stale.out").write_text("leftover", encoding="utf-8")
        assert _ignored_snapshot(path)

        restored = await _run(
            executor, RestoreRevision(**_common(attempt, "r"), attempt=attempt, revision=revision)
        )
        assert _status(restored) is ObservationStatus.SUCCEEDED
        assert _ignored_snapshot(path) == {}
        assert not (path / "stale.out").exists()
        assert (path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 2\n"

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_f3_restart_between_binding_and_result_reattaches_instead_of_rejecting(
    tmp_path: Path,
) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            first_host = env.hosts[0]
            executor, _ = _executor(first_host)
            attempt = _attempt()
            request = _ensure(first_host, attempt, "e1")
            await _run(executor, request)  # the host dies before its result is recorded
            (path,) = _worktree_paths(first_host)
            (path / "work.txt").write_text("in progress\n", encoding="utf-8")

            second_host = env.start_host()
            restarted, _ = _executor(second_host)
            again = await _run(restarted, request, epoch=2, repeat=True)
            assert _status(again) is ObservationStatus.SUCCEEDED
            assert _worktree_paths(second_host) == {path}
            assert (path / "work.txt").read_text(encoding="utf-8") == "in progress\n"
            # Every attempt request works on the reattached workspace; none is a stored rejection.
            revision = await _snapshot_of(restarted, attempt, "s1", epoch=2)
            restored = await _run(
                restarted,
                RestoreRevision(**_common(attempt, "r1"), attempt=attempt, revision=revision),
                epoch=2,
            )
            assert _status(restored) is ObservationStatus.SUCCEEDED
            assert (path / "work.txt").read_text(encoding="utf-8") == "in progress\n"

        asyncio.run(exercise())


def test_f3_restart_after_a_recorded_result_still_serves_the_attempt(tmp_path: Path) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            first_host = env.hosts[0]
            executor, _ = _executor(first_host)
            attempt = _attempt()
            await _ensure_at(executor, first_host, attempt, "e1")
            second_host = env.start_host()
            restarted, _ = _executor(second_host)
            snapshot = await _run(
                restarted,
                SnapshotAndRetain(**_common(attempt, "s1"), attempt=attempt, retention="wip"),
                epoch=2,
            )
            assert _status(snapshot) is ObservationStatus.SUCCEEDED

        asyncio.run(exercise())


@settings(
    max_examples=examples(pr=3, nightly=10),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    edits=st.lists(st.booleans(), min_size=1, max_size=3),
    scratch=st.booleans(),
    root_moved=st.booleans(),
)
def test_a_clean_host_stop_keeps_every_attempt_workspace_for_the_next_host(
    tmp_path_factory: pytest.TempPathFactory, edits: list[bool], *, scratch: bool, root_moved: bool
) -> None:
    """A stop while attempts implement must not discard their worktrees.

    Core still holds each attempt across a stop (its cancel and access settlement run on the
    next host), so the next host must reopen every attempt workspace with its work in it.
    Only scratch candidates, which no attempt holds, go with the host. Regression: a clean
    stop removed every attempt worktree, so each resumed cancel's settlement found no
    workspace, never became terminal, and left recovery blocked.
    """
    with _env(tmp_path_factory.mktemp("stop")) as env:

        async def exercise() -> None:
            first_host = env.hosts[0]
            executor, _ = _executor(first_host)
            attempts = [_attempt(f"a{index}") for index in range(len(edits))]
            paths = [
                await _ensure_at(executor, first_host, attempt, f"e-{attempt.attempt_id.root}")
                for attempt in attempts
            ]
            for path, edited in zip(paths, edits, strict=True):
                if edited:
                    (path / "work.txt").write_text(f"in progress {path.name}\n", encoding="utf-8")
            expected = [_visible_files(path) for path in paths]
            if scratch:
                await first_host.create_candidate()
            root = first_host.root.path
            await first_host.close()
            if root_moved:
                # A resume over changed code: the root has moved on since the attempts began.
                (root / "candidate.py").write_text("VALUE = 3\n", encoding="utf-8")
                _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "change")

            second_host = env.start_host()
            assert _worktree_paths(second_host) == set(paths)
            restarted, _ = _executor(second_host)
            for attempt, path, files in zip(attempts, paths, expected, strict=True):
                name = attempt.attempt_id.root
                revision = await _snapshot_of(restarted, attempt, f"s-{name}", "wip", epoch=2)
                assert _visible_files(path) == files
                restored = await _run(
                    restarted,
                    RestoreRevision(
                        **_common(attempt, f"r-{name}"), attempt=attempt, revision=revision
                    ),
                    epoch=2,
                )
                assert _status(restored) is ObservationStatus.SUCCEEDED
                assert _visible_files(path) == files
            assert _worktree_paths(second_host) == set(paths)

        asyncio.run(exercise())


def _candidates_on_disk(env: _Env) -> set[Path]:
    """The candidate worktree directories of the run, registered or not."""
    worktrees = env.project.state.candidate_worktrees_directory(env.run_id)
    if not worktrees.is_dir():
        return set()
    return {entry / "workspace" for entry in worktrees.iterdir() if (entry / "workspace").exists()}


def _registered(root: Path) -> set[Path]:
    """The linked worktrees the run's repository registers, its root aside."""
    paths = {Path(line.split()[0]) for line in _git(root, "worktree", "list").splitlines()}
    return paths - {root.resolve()}


@settings(
    max_examples=examples(pr=4, nightly=16),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(
    held=st.lists(st.booleans(), min_size=1, max_size=3),
    final=st.booleans(),
    scratch=st.booleans(),
    restart=st.booleans(),
)
def test_a_host_stop_keeps_exactly_the_attempt_workspaces_core_still_holds(
    tmp_path_factory: pytest.TempPathFactory,
    held: list[bool],
    *,
    final: bool,
    scratch: bool,
    restart: bool,
) -> None:
    """After a final result no attempt worktree or registration is left; else core's stay.

    After a resumable stop every workspace core references stays on disk with its work, and
    every other one is gone. With ``restart`` the stopping host never learned core's state,
    so it kept every attempt workspace, and the next host's start sweeps the ones core no
    longer references. Regression: a close kept every attempt workspace whatever core's
    state, so a run with a final result (a deadline stop with attempts open), and members no
    later host reattached, leaked their worktrees and Git registrations.
    """
    with _env(tmp_path_factory.mktemp("holds")) as env:

        async def exercise() -> None:
            first_host = env.hosts[0]
            root = first_host.root.path
            executor, _ = _executor(first_host)
            attempts = [_attempt(f"a{index}") for index in range(len(held))]
            paths = [
                await _ensure_at(executor, first_host, attempt, f"e-{attempt.attempt_id.root}")
                for attempt in attempts
            ]
            for path in paths:
                (path / "work.txt").write_text(f"in progress {path.name}\n", encoding="utf-8")
            work = {path: _visible_files(path) for path in paths}
            if scratch:
                await first_host.create_candidate()
            holds = WorkspaceHolds(
                final=final,
                attempts=frozenset(
                    attempt for attempt, kept in zip(attempts, held, strict=True) if kept
                ),
            )
            if restart:
                await first_host.close()
                next_host = env.start_host()
                next_host.follow(lambda: holds)
                await next_host.release_unheld()
                await next_host.close()
            else:
                first_host.follow(lambda: holds)
                await first_host.close()
            expected = (
                set() if final else {path for path, kept in zip(paths, held, strict=True) if kept}
            )
            assert _registered(root) == expected
            assert _candidates_on_disk(env) == expected
            for path in expected:
                assert _visible_files(path) == work[path]

        asyncio.run(exercise())


def test_an_unsettled_command_candidate_survives_followed_close_and_restart(
    tmp_path: Path,
) -> None:
    """An unconfirmed Slurm command is durable cleanup intent, not a core attempt hold."""
    with _env(tmp_path, recoverable_capture=True) as env:

        async def exercise() -> None:
            member_id = "capture-unconfirmed-operation"
            first = env.hosts[0]
            candidate = await first.create_candidate(member_id=member_id)
            path = candidate.path
            await first.retain_unsettled(candidate)
            first.follow(lambda: WorkspaceHolds(final=True, attempts=frozenset()))
            await first.close()

            assert path in _candidates_on_disk(env)
            second = env.start_host()
            second.follow(lambda: WorkspaceHolds(final=True, attempts=frozenset()))
            await second.release_unheld()
            assert path in _candidates_on_disk(env)
            with pytest.raises(RuntimeContractError, match="retained by the previous host"):
                await second.create_candidate(member_id=member_id)

        asyncio.run(exercise())


def test_f4_exclusive_root_is_held_by_one_attempt_generation(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        a, b = _attempt("a"), _attempt("b")
        first = await _run(executor, _ensure(workspaces, a, "ea", WorkspaceMode.EXCLUSIVE_ROOT))
        assert _status(first) is ObservationStatus.SUCCEEDED
        other = await _run(executor, _ensure(workspaces, b, "eb", WorkspaceMode.EXCLUSIVE_ROOT))
        assert _status(other) is ObservationStatus.UNKNOWN
        assert not other.observation.observation.terminal
        # A restarted executor sees the same holder.
        restarted, _ = _executor(workspaces)
        again = await _run(
            restarted, _ensure(workspaces, b, "eb2", WorkspaceMode.EXCLUSIVE_ROOT), epoch=2
        )
        assert _status(again) is ObservationStatus.UNKNOWN
        # A newer generation of the holder supersedes it; the old generation is then refused.
        newer = _attempt("a", 1)
        granted = await _run(
            restarted, _ensure(workspaces, newer, "ea1", WorkspaceMode.EXCLUSIVE_ROOT), epoch=2
        )
        assert _status(granted) is ObservationStatus.SUCCEEDED
        stale = await _run(
            restarted,
            SnapshotAndRetain(**_common(a, "stale"), attempt=a, retention="wip"),
            epoch=2,
        )
        assert _status(stale) is ObservationStatus.REJECTED
        old = await _run(
            restarted,
            _ensure(workspaces, _attempt("a", 0), "ea0b", WorkspaceMode.EXCLUSIVE_ROOT),
            epoch=2,
        )
        assert _status(old) is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


@pytest.mark.parametrize("request_kind", ["snapshot", "restore", "discard", "ensure"])
def test_a_lower_generation_is_rejected_once_a_higher_one_exists(
    tmp_path: Path, request_kind: str
) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        old, new = _attempt("a", 0), _attempt("a", 1)
        path = await _ensure_at(executor, workspaces, old, "e0")
        (path / "old.txt").write_text("generation 0\n", encoding="utf-8")
        revision = await _snapshot_of(executor, old, "s0")
        await _run(executor, _ensure(workspaces, new, "e1"))
        worktrees = _worktrees(workspaces)
        stale: Request = {
            "snapshot": SnapshotAndRetain(**_common(old, "x"), attempt=old, retention="wip"),
            "restore": RestoreRevision(**_common(old, "x"), attempt=old, revision=revision),
            "discard": DiscardWorkspace(**_common(old, "x"), attempt=old),
            "ensure": _ensure(workspaces, old, "x"),
        }[request_kind]
        result = await _run(executor, stale)
        assert _status(result) is ObservationStatus.REJECTED
        assert _worktrees(workspaces) == worktrees
        # A revision the old generation recorded is not restorable by the new one.
        foreign = await _run(
            executor, RestoreRevision(**_common(new, "r"), attempt=new, revision=revision)
        )
        assert _status(foreign) is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


def test_f6_foreign_dangling_and_unretained_revisions_are_rejected(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        a, b = _attempt("a"), _attempt("b")
        await _ensure_at(executor, workspaces, a, "ea")
        path_b = await _ensure_at(executor, workspaces, b, "eb")
        (path_b / "candidate.py").write_text("VALUE = 'B'\n", encoding="utf-8")
        revision_b = await _snapshot_of(executor, b, "sb")

        def restore(name: str, revision: RevisionRef) -> RestoreRevision:
            return RestoreRevision(**_common(a, name), attempt=a, revision=revision)

        foreign = await _run(executor, restore("foreign", revision_b))
        assert _status(foreign) is ObservationStatus.REJECTED
        tree = _git(workspaces.root.path, "rev-parse", "HEAD^{tree}")
        dangling = revision_ref(
            _git(
                workspaces.root.path,
                *("-c", "user.name=test", "-c", "user.email=test@example.com"),
                *("commit-tree", tree, "-m", "dangling"),
            )
        )
        assert _status(await _run(executor, restore("dangling", dangling))) is (
            ObservationStatus.REJECTED
        )
        assert _status(await _run(executor, _adopt("adopt", dangling, AdoptRevision))) is (
            ObservationStatus.REJECTED
        )
        # Once B's revision is retained for the run, A may use it.
        retained = await _run(
            executor,
            RetainRevision(**_common(b, "retain"), attempt=b, revision=revision_b, retention="wip"),
        )
        assert _status(retained) is ObservationStatus.SUCCEEDED
        restarted, _ = _executor(workspaces)
        assert _status(await _run(restarted, restore("shared", revision_b), epoch=2)) is (
            ObservationStatus.SUCCEEDED
        )

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))


class _FaultyResources:
    """The real provider with failures injected where the host would see them."""

    def __init__(self, inner: WorkspaceResourceProvider) -> None:
        self._inner = inner
        self.fail_creates = 0
        self.fail_closes = 0
        self.hold_close: tuple[Event, Event] | None = None
        """When set, a close sets the first event and waits for the second."""

    @property
    def root(self) -> WorkspaceResource:
        return self._inner.root

    @property
    def supports_parallel_candidates(self) -> bool:
        return self._inner.supports_parallel_candidates

    def create_candidate(
        self, workspace_id: str, revision: str, _step_tag: str | None = None, /
    ) -> WorkspaceResource:
        if self.fail_creates:
            self.fail_creates -= 1
            message = "transient candidate creation failure"
            raise RuntimeContractError(message)
        return cast(
            "WorkspaceResource",
            _FaultyResource(self, self._inner.create_candidate(workspace_id, revision)),
        )

    def reattach_candidate(self, workspace_id: str, revision: str, /) -> WorkspaceResource | None:
        resource = self._inner.reattach_candidate(workspace_id, revision)
        return (
            None if resource is None else cast("WorkspaceResource", _FaultyResource(self, resource))
        )

    def leftover_candidates(self) -> tuple[str, ...]:
        return self._inner.leftover_candidates()

    def unsettled_candidates(self) -> frozenset[str]:
        return self._inner.unsettled_candidates()

    def record_capture_intent(
        self, workspace_id: str, execution: CaptureExecutionIdentity, /
    ) -> None:
        self._inner.record_capture_intent(workspace_id, execution)

    def clear_capture_intent(self, workspace_id: str, /) -> None:
        self._inner.clear_capture_intent(workspace_id)

    def reconcile_capture_intents(self) -> frozenset[str]:
        return self._inner.reconcile_capture_intents()

    def remove_leftover(self, workspace_id: str, /) -> None:
        self._inner.remove_leftover(workspace_id)


class _FaultyResource:
    def __init__(self, owner: _FaultyResources, resource: WorkspaceResource) -> None:
        self._owner = owner
        self._resource = resource

    def __getattr__(self, name: str) -> object:
        return getattr(self._resource, name)

    def close(self) -> None:
        if self._owner.fail_closes:
            self._owner.fail_closes -= 1
            message = "worktree removal failed"
            raise OSError(message)
        if self._owner.hold_close is not None:
            entered, release = self._owner.hold_close
            entered.set()
            wait_or_fail(release, "the held close release")
        self._resource.close()


def _faulty(factory: WorkspaceResourceProvider) -> _FaultyResources:
    return _FaultyResources(factory)


def _faults(env: _Env) -> _FaultyResources:
    assert isinstance(env.factory, _FaultyResources)
    return env.factory


class _HoldingCloseRunner:
    """Cooperatively hold resource close while unrelated blocking calls can run."""

    def __init__(self, entered: Gate, release: Gate) -> None:
        self._entered = entered
        self._release = release
        self._enabled = False

    def enable(self) -> None:
        self._enabled = True

    async def run[**P, Result](
        self,
        function: Callable[P, Result],
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Result:
        if self._enabled and getattr(function, "__name__", None) == "close":
            self._entered.open()
            await self._release.wait()
        return function(*args, **kwargs)


def test_f7_a_failed_discard_is_not_stored_and_a_retry_completes_it(tmp_path: Path) -> None:
    with _env(tmp_path, _faulty) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            executor, _ = _executor(workspaces)
            attempt = _attempt()
            path = await _ensure_at(executor, workspaces, attempt, "e1")
            discard = DiscardWorkspace(**_common(attempt, "d1"), attempt=attempt)
            _faults(env).fail_closes = 1
            first = await _run(executor, discard)
            assert _status(first) is ObservationStatus.FAILED
            assert not first.observation.observation.terminal
            second = await _run(executor, discard)
            observation = second.observation.observation
            assert _status(second) is ObservationStatus.SUCCEEDED
            assert observation.released
            assert observation.children_complete
            assert not path.exists()

        asyncio.run(exercise())


def test_f8_a_crash_between_the_discard_and_its_record_recovers_to_complete(
    tmp_path: Path,
) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            crashing = _failing(env)
            executor, _ = _executor(workspaces, crashing)
            attempt = _attempt()
            path = await _ensure_at(executor, workspaces, attempt, "e1")
            discard = DiscardWorkspace(**_common(attempt, "d1"), attempt=attempt)
            crashing.fail_release_mark = True
            with pytest.raises(OSError, match="host died"):
                await _run(executor, discard)
            assert not path.exists()
            crashing.fail_release_mark = False
            again = await _run(_executor(workspaces)[0], discard, epoch=2, repeat=True)
            observation = again.observation.observation
            assert observation.released
            assert observation.children_complete
            assert _status(await _run(_executor(workspaces)[0], discard, epoch=2, repeat=True)) is (
                ObservationStatus.SUCCEEDED
            )

        asyncio.run(exercise())


def test_f8_a_release_by_anyone_else_does_not_claim_completeness(tmp_path: Path) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            executor, _ = _executor(workspaces)
            attempt = _attempt()
            path = await _ensure_at(executor, workspaces, attempt, "e1")
            shutil.rmtree(path)
            _git(workspaces.root.path, "worktree", "prune")
            restarted, _ = _executor(env.start_host())
            discard = DiscardWorkspace(**_common(attempt, "d1"), attempt=attempt)
            observation = (await _run(restarted, discard, epoch=2)).observation.observation
            assert observation.released
            assert not observation.children_complete

        asyncio.run(exercise())


_WITHDRAW_CASES = tuple(
    itertools.product(
        (WorkspaceMode.ISOLATED_CHILD, WorkspaceMode.EXCLUSIVE_ROOT),
        (("ensure", "discard"), ("discard", "ensure")),
        (False, True),  # the first ensure's host dies after making the workspace, before binding
        (False, True),  # the ensure is retried at the end
    )
)


def test_a_discard_answered_as_nothing_held_is_never_followed_by_a_workspace(
    tmp_path: Path,
) -> None:
    """Every order of ensure and discard of one attempt, over the same disk.

    A discard that finds no binding fences the generation and removes any worktree an
    interrupted ensure left: it holds nothing, and an ensure that arrives later is refused.
    A discard after a bound ensure releases what it bound. Either way no worktree survives.
    Each case runs on its own attempt of one run: a pull request samples six evenly spaced
    cases (both modes, both orders, with and without the crash), nightly runs all sixteen.
    """
    with _env(tmp_path) as env:

        async def case(
            index: int, mode: WorkspaceMode, order: tuple[str, ...], *, crash: bool, retry: bool
        ) -> None:
            attempt = _attempt(f"a{index}")
            ensure = _ensure(env.hosts[0], attempt, f"e{index}", mode)
            discard = DiscardWorkspace(**_common(attempt, f"d{index}"), attempt=attempt)
            answers: dict[str, ExecutionResult] = {}
            host = env.hosts[-1]
            for step in (*order, *(("retry",) if retry else ())):
                store = _failing(env)
                store.fail_bind = crash and step == "ensure"
                executor, _ = _executor(host, store)
                try:
                    answers[step] = await _run(executor, discard if step == "discard" else ensure)
                except OSError:
                    assert store.fail_bind
                    host = env.start_host()  # the host died: the next step runs on a new one
            withdrawn = answers["discard"].observation.observation
            if withdrawn.status is ObservationStatus.REJECTED:
                assert (withdrawn.released, withdrawn.children_complete) == (True, True)
                assert withdrawn.resource_id is None
                ensures = (answer for step, answer in answers.items() if step != "discard")
                assert all(_status(answer) is ObservationStatus.REJECTED for answer in ensures)
            else:
                assert _status(answers["ensure"]) is ObservationStatus.SUCCEEDED
                assert withdrawn.released
            assert _worktree_paths(env.hosts[-1]) == set()

        async def exercise() -> None:
            for index in swept(len(_WITHDRAW_CASES), 6):
                mode, order, crash, retry = _WITHDRAW_CASES[index]
                await case(index, mode, order, crash=crash, retry=retry)

        asyncio.run(exercise())


def test_f9_interrupted_restore_is_completed_or_confirmed_on_retry(tmp_path: Path) -> None:
    with _env(tmp_path) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            executor, _ = _executor(workspaces)
            attempt = _attempt()
            path = await _ensure_at(executor, workspaces, attempt, "e1")
            (path / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
            base = revision_ref(workspaces.root.revision or "")
            restore = RestoreRevision(**_common(attempt, "r1"), attempt=attempt, revision=base)
            await _run(executor, restore)  # the host dies before its result is recorded
            restarted, _ = _executor(workspaces)
            result = await _run(restarted, restore, epoch=2, repeat=True)
            assert _status(result) is ObservationStatus.SUCCEEDED
            assert (path / "candidate.py").read_text(encoding="utf-8") == "VALUE = 1\n"

        asyncio.run(exercise())


def test_f13_a_transient_create_failure_is_retried_not_stored(tmp_path: Path) -> None:
    with _env(tmp_path, _faulty) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            executor, _ = _executor(workspaces)
            request = _ensure(workspaces, _attempt(), "e1")
            _faults(env).fail_creates = 1
            failed = await _run(executor, request)
            assert _status(failed) is ObservationStatus.FAILED
            retried = await _run(executor, request)
            assert _status(retried) is ObservationStatus.SUCCEEDED

        asyncio.run(exercise())


def test_f12_a_slow_discard_does_not_block_another_attempt(tmp_path: Path, sim: Sim) -> None:
    entered, release = sim.gate(), sim.gate()
    runner = _HoldingCloseRunner(entered, release)
    with _env(tmp_path, _faulty, blocking=runner) as env:

        async def exercise() -> None:
            workspaces = env.hosts[0]
            executor, _ = _executor(workspaces)
            a, b = _attempt("a"), _attempt("b")
            await _ensure_at(executor, workspaces, a, "ea")
            await _ensure_at(executor, workspaces, b, "eb")
            runner.enable()
            slow = asyncio.create_task(
                _run(executor, DiscardWorkspace(**_common(a, "da"), attempt=a))
            )
            await entered.wait(slow)
            base = revision_ref(workspaces.root.revision or "")
            other = await _run(
                executor, RestoreRevision(**_common(b, "rb"), attempt=b, revision=base)
            )
            assert _status(other) is ObservationStatus.SUCCEEDED
            release.open()
            assert _status(await slow) is ObservationStatus.SUCCEEDED
            # The hosts' blocking work runs on the simulated threads, so they close here.
            await workspaces.close()

        try:
            sim.run(exercise())
        finally:
            # A failed simulation has already closed its thread driver; the ordinary
            # context cleanup cannot drive this host's injected blocking runner again.
            env.hosts.clear()


def test_unsupported_requests_get_a_typed_observation_never_a_refusal(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        executor, _ = _executor(workspaces)
        attempt = _attempt()
        close = CloseAttemptScope(**_common(attempt, "close"), attempt=attempt)
        outcome = await executor.execute(close, _context(close))
        assert isinstance(outcome, ExecutionResult)
        assert _status(outcome) is ObservationStatus.REJECTED

    with _workspaces(tmp_path) as workspaces:
        asyncio.run(exercise(workspaces))
