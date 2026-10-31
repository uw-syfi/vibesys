"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Never, TypeAlias, TypeVar

from pydantic import BaseModel

from vs_agent.api import (
    NULL_SKILL_SELECTION,
)
from vs_core.api import AdoptionPublication, CheckoutStatus, CheckoutUpdate
from vs_project.api import Git, adoption_branch
from vs_runtime._local_validation import LocalValidationRecipeError, check_recipe_artifact_path
from vs_runtime._trusted_evaluation import (
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    TrustedBuildResult,
)
from vs_runtime._workspace_access import WorkspaceAccessRecovery
from vs_runtime.contracts import (
    AccuracyEvaluation,
    AccuracyReceipt,
    BenchmarkEvaluation,
    BenchmarkObjective,
    BuildEvaluation,
    CandidateWorkspace,
    CommandResult,
    LocalValidationEvaluation,
    RuntimeContractError,
    SessionClosedError,
    Workspace,
    WorkspaceAccess,
    WorkspaceRestoreError,
    member_workspace_id,
    validate_command,
    validate_objectives,
    validate_trusted_shell_command,
    validate_workspace_writable_paths,
)
from vs_sim.api import DISCARD, OutputStream
from vs_sim.api.testing import FakeProcesses, ProcessScript

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import CapacityStops
    from vs_runtime._agent_lifecycle import AgentExecutionLifecycleEvent
    from vs_runtime._run_control import (
        CapacityWaitEnd,
        PauseWake,
        RunControlChannel,
        RunControlTransition,
    )
    from vs_runtime._workspace_holds import WorkspaceHolds
    from vs_sandbox.api import CommandRunner, HostResource, ProjectPathPolicy
    from vs_sim.api import OutputSink, ProcessSpec, Threads


class FakeAgentExecutionEnvironment:
    """In-memory scoped environment with observable idempotent cleanup."""

    def __init__(
        self,
        *,
        project_path_policy: ProjectPathPolicy,
        host_resources: tuple[HostResource, ...] = (),
        use_docker: bool = False,
        close_log: list[str] | None = None,
        name: str = "environment",
    ) -> None:
        """Configure environment facts and optional cleanup recording."""
        self.skill_source_dirs: tuple[Path, ...] = ()
        self.skill_selection = NULL_SKILL_SELECTION
        self.project_path_policy = project_path_policy
        self.host_resources = host_resources
        self.backends: dict[str, CommandRunner] | None = None
        self.use_docker = use_docker
        self.closed = False
        self._close_log = close_log
        self._name = name

    def close(self) -> None:
        """Record cleanup exactly once."""
        if self.closed:
            return
        self.closed = True
        if self._close_log is not None:
            self._close_log.append(self._name)

    def agent_path(self, host_path: Path | str) -> str:
        """Return the identity mapping used by the in-memory environment."""
        return str(Path(host_path))


class FakeAgentExecutionLifecycleSink:
    """Record semantic execution events in emission order."""

    def __init__(self) -> None:
        """Create an empty lifecycle log."""
        self.events: list[AgentExecutionLifecycleEvent] = []

    def __call__(self, event: AgentExecutionLifecycleEvent) -> None:
        """Record one semantic lifecycle event."""
        self.events.append(event)


@dataclass(frozen=True, slots=True)
class FakeTrustedAccuracyCall:
    """One trusted accuracy invocation observed by the Fake."""

    command_override: str | None


@dataclass(frozen=True, slots=True)
class FakeTrustedBenchmarkCall:
    """One trusted benchmark invocation observed by the Fake."""

    command_override: str | None
    required_metrics: frozenset[str]


class FakeTrustedEvaluationExecutor:
    """Scripted in-memory trusted evaluation mechanism."""

    def __init__(self) -> None:
        """Create an executor with passing, unexecuted defaults."""
        self.build_confinements: list[CommandRunner | None] = []
        self.accuracy_calls: list[FakeTrustedAccuracyCall] = []
        self.benchmark_calls: list[FakeTrustedBenchmarkCall] = []
        self._accuracy_results: list[TrustedAccuracyResult] = []
        self._benchmark_results: list[TrustedBenchmarkResult] = []

    def script_accuracy(self, *results: TrustedAccuracyResult) -> None:
        """Replace queued accuracy outcomes."""
        self._accuracy_results = list(results)

    def script_benchmark(self, *results: TrustedBenchmarkResult) -> None:
        """Replace queued benchmark outcomes."""
        self._benchmark_results = list(results)

    async def build(
        self, *, confinement: CommandRunner | None, output: OutputSink
    ) -> TrustedBuildResult:
        """Record one call and pass without running anything; it prints nothing."""
        del output
        self.build_confinements.append(confinement)
        return TrustedBuildResult(executed=False, passed=True)

    async def accuracy(
        self, *, command_override: str | None = None, output: OutputSink
    ) -> TrustedAccuracyResult:
        """Record one call and return the next scripted outcome; it prints nothing."""
        del output
        self.accuracy_calls.append(FakeTrustedAccuracyCall(command_override))
        if self._accuracy_results:
            return self._accuracy_results.pop(0)
        return TrustedAccuracyResult(executed=False, passed=True)

    async def benchmark(
        self,
        *,
        command_override: str | None = None,
        required_metrics: frozenset[str] = frozenset(),
        output: OutputSink,
    ) -> TrustedBenchmarkResult:
        """Record one call and return the next scripted outcome; it prints nothing."""
        del output
        self.benchmark_calls.append(FakeTrustedBenchmarkCall(command_override, required_metrics))
        if self._benchmark_results:
            return self._benchmark_results.pop(0)
        return TrustedBenchmarkResult(executed=False, passed=True)


ResponseT = TypeVar("ResponseT", bound=BaseModel)


class FakeRunControlEventSink:
    """Record synchronous run-control transitions in emission order."""

    def __init__(
        self,
        *,
        on_transition: Callable[[RunControlTransition], None] | None = None,
    ) -> None:
        """Configure an optional deterministic reaction to each transition."""
        self.transitions: list[RunControlTransition] = []
        self._on_transition = on_transition

    def __call__(self, transition: RunControlTransition) -> None:
        """Record one transition and invoke the configured reaction."""
        self.transitions.append(transition)
        if self._on_transition is not None:
            self._on_transition(transition)


class FakeCapacityTimer:
    """A virtual clock for the capacity gate: waits cost no real time.

    ``pause_for_quota`` advances the clock by the requested seconds and lets the real
    control channel settle the wait with a timed resume due at once, so the channel's
    PAUSED and RESUMED transitions are the production ones. With ``operator_resumes_after`` set,
    the operator resumes the run that many virtual seconds into any wait that lasts
    longer.
    """

    def __init__(
        self,
        control: RunControlChannel,
        *,
        start: float = 1_000_000.0,
        operator_resumes_after: float | None = None,
        during_capacity_wait: Callable[[float], bool] | None = None,
    ) -> None:
        """Start the virtual clock at ``start`` epoch seconds.

        ``during_capacity_wait`` is the operator during a wait for capacity: called with the
        wait's length, it returns True when it acted (resumed, paused or asked again).
        """
        self._control = control
        self._now = start
        self._operator_after = operator_resumes_after
        self._during_capacity_wait = during_capacity_wait
        self.waits: list[float] = []
        self.capacity_waits: list[float] = []

    def now(self) -> float:
        """Return the virtual epoch second."""
        return self._now

    def wait_for_capacity(
        self, seconds: float, request: int, turn: CapacityStops
    ) -> CapacityWaitEnd:
        """Let ``during_capacity_wait`` act as the operator, else spend ``seconds`` of virtual time.

        The real channel then settles the wait with a zero timeout, so its outcome and its
        transitions are the production ones. A cancelled ``turn`` spends no time.
        """
        self.capacity_waits.append(seconds)
        acted = self._during_capacity_wait is not None and self._during_capacity_wait(seconds)
        if not acted and not turn.cancelled:
            self._now += seconds
        return self._control.wait_for_capacity(0.0, request, ended=lambda: turn.cancelled)

    def pause_for_quota(self, seconds: float, turn: CapacityStops) -> PauseWake:
        """Pause the run and spend ``seconds`` of virtual time, or fewer when the operator resumes first.

        A cancelled ``turn`` spends no time and leaves the timed resume armed ``seconds``
        ahead on the channel's own clock, as production does.
        """
        self.waits.append(seconds)
        operator_first = self._operator_after is not None and self._operator_after < seconds
        if turn.cancelled:
            self._control.pause_for_quota(seconds)
        elif operator_first and self._operator_after is not None:
            self._control.pause_for_quota(seconds)
            self._now += self._operator_after
            self._control.resume()
        else:
            self._control.pause_for_quota(0.0)
            self._now += seconds
        return self._control.wait_resumed(ended=lambda: turn.cancelled)

    def park_for_capacity(self, turn: CapacityStops, *, waiting: bool) -> int | PauseWake:
        """Park on the real channel, as production does; a cancelled ``turn`` ends the park."""
        return self._control.park_for_capacity(waiting=waiting, ended=lambda: turn.cancelled)

    def wait_out(self, turn: CapacityStops, seconds: float) -> None:
        """Spend ``seconds`` of virtual time holding one turn; ``waits`` records it.

        Nothing ends the wait sooner: a test that cancels or stops a turn mid-wait runs the
        production timer on the simulated threads instead.
        """
        del turn
        self.waits.append(seconds)
        self._now += seconds


class FakeProjectMaterializationEffects:
    """Deterministic environment effects for project materialization tests."""

    def __init__(self, *, isolated: bool = False, removes_children: bool = True) -> None:
        """Configure symlink and privileged-removal behavior."""
        self._isolated = isolated
        self.removes_children = removes_children
        self.repaired: list[Path] = []
        self.removed: list[tuple[Path, str]] = []

    @property
    def isolated(self) -> bool:
        """Return the configured symlink policy."""
        return self._isolated

    def repair(self, workspace: Path) -> None:
        """Record a requested permission repair."""
        self.repaired.append(workspace)

    def remove_child(self, workspace: Path, name: str) -> bool:
        """Record and return the configured privileged-removal outcome."""
        self.removed.append((workspace, name))
        return self.removes_children


def _answers_take_no_time(seconds: float) -> Never:
    message = f"an in-memory Git answer took {seconds:g} s"
    raise AssertionError(message)


class FakeGitRunner:
    """Git for pinned project-source checkouts, answered in memory: ``git()`` starts no process.

    Any other command fails as it would outside a repository.
    """

    def __init__(self, *, head: str, fail_command: str | None = None) -> None:
        """Configure the resolved commit and optional failing Git command."""
        self.head = head
        self.fail_command = fail_command
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []

    def git(self) -> Git:
        """A ``Git`` whose every command this Fake answers."""
        return Git(FakeProcesses(self._answer, pass_time=_answers_take_no_time))

    def _answer(self, spec: ProcessSpec) -> ProcessScript:
        """Apply clone/checkout/rev-parse semantics without a subprocess."""
        args = spec.argv[1:]
        command = args[0]
        self.calls.append((args, spec.cwd))
        if command == self.fail_command:
            return ProcessScript(returncode=1, stderr=b"configured fake failure")
        if command == "clone":
            (Path(args[-1]) / ".git").mkdir(parents=True)
            return ProcessScript()
        if command == "checkout":
            return ProcessScript()
        if command == "rev-parse":
            return ProcessScript(stdout=f"{self.head}\n".encode())
        return ProcessScript(returncode=128, stderr=b"fatal: not a git repository")


@dataclass(frozen=True, slots=True)
class FakeModelVolumeRequest:
    """One model-volume effect observed by a fake provisioner."""

    model_id: str
    revision: str | None


class FakeModelVolumeProvisioner:
    """Deterministic in-memory model-volume provisioner for composition tests."""

    def __init__(self) -> None:
        """Create an empty successful provisioner."""
        self._requests: list[FakeModelVolumeRequest] = []
        self._volumes: dict[FakeModelVolumeRequest, str] = {}
        self._failure: Exception | None = None

    @property
    def requests(self) -> tuple[FakeModelVolumeRequest, ...]:
        """Return provision attempts in call order."""
        return tuple(self._requests)

    def fail_with(self, error: Exception | None) -> None:
        """Configure an error for subsequent provision attempts."""
        self._failure = error

    def __call__(
        self,
        model_id: str,
        *,
        revision: str | None = None,
        log: Callable[[str], object] = print,
    ) -> str:
        """Record one request and return a stable volume name for its identity."""
        del log
        request = FakeModelVolumeRequest(model_id=model_id, revision=revision)
        self._requests.append(request)
        if self._failure is not None:
            raise self._failure
        if request not in self._volumes:
            self._volumes[request] = f"fake-model-volume-{len(self._volumes) + 1}"
        return self._volumes[request]


class _UnknownWorkspaceRevisionError(ValueError):
    def __init__(self, revision: str) -> None:
        super().__init__(f"workspace revision is not retained: {revision!r}")


class _WorkspaceRetentionLabelError(ValueError):
    def __init__(self) -> None:
        super().__init__("workspace retention label must be nonempty")


@dataclass(frozen=True)
class _FakeCandidateConfig:
    """Creation state copied into one isolated fake workspace."""

    workspace_id: str
    path: Path
    revision: str
    trusted_input_baseline: str | None
    revision_prefix: str


class FakeWorkspaces:
    """In-memory owner of one root and its isolated candidate workspaces."""

    def __init__(
        self,
        root: FakeWorkspace,
        *,
        supports_parallel_candidates: bool = False,
    ) -> None:
        """Bind the fake capability to one root and a fixed isolation capability."""
        self._root = root
        self._supports_parallel_candidates = supports_parallel_candidates
        self._candidates: list[FakeCandidateWorkspace] = []
        self._patches: dict[str, str] = {}
        self._default_patch: str | None = None
        self._candidate_retains: list[tuple[BaseException | None, bool]] = []
        self.export_patch_calls: list[str] = []
        self._closing = False
        self._closed = False
        self._holds: Callable[[], WorkspaceHolds] | None = None
        self._measured: list[CandidateWorkspace] = []
        self._measured_step_tags: list[str | None] = []

    def script_candidate_retain(
        self, *results: BaseException | None, after_retention: bool = False
    ) -> None:
        """Script retention acknowledgements for the next created candidate."""
        self._candidate_retains.extend((result, after_retention) for result in results)

    @property
    def root(self) -> FakeWorkspace:
        """Return the configured fake root workspace."""
        return self._root

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return the fixed candidate-isolation capability."""
        return self._supports_parallel_candidates

    def host_died(self) -> None:
        """The host dies: live candidates stay on disk with no handle to release them."""
        for candidate in self._candidates:
            candidate.orphaned = not candidate.discarded

    def leaked_candidates(self) -> tuple[FakeCandidateWorkspace, ...]:
        """Worktrees on disk that no live handle owns."""
        return tuple(c for c in self._candidates if c.orphaned and not c.discarded)

    @property
    def candidates(self) -> tuple[FakeCandidateWorkspace, ...]:
        """Return candidates in creation order, including discarded ones."""
        return tuple(self._candidates)

    async def create_candidate(
        self,
        from_revision: str | None = None,
        *,
        member_id: str | None = None,
    ) -> CandidateWorkspace:
        """Create one isolated workspace from a known root revision."""
        if self._closing or self._closed:
            raise SessionClosedError
        if not self._supports_parallel_candidates:
            message = "this run does not support parallel candidate workspaces"
            raise RuntimeContractError(message)
        revision = from_revision or self._root.revision
        if revision is None or not self._root.knows_revision(revision):
            raise WorkspaceRestoreError(from_revision or "")
        if member_id is None:
            workspace_id = f"candidate-{len(self._candidates) + 1}"
        else:
            workspace_id = member_workspace_id(member_id)
            for leftover in self.leaked_candidates():
                if leftover.id == workspace_id:  # a restarted host replaces a dead one's worktree
                    leftover.discard_leftover()
            if any(not c.discarded and c.id == workspace_id for c in self._candidates):
                message = f"member {member_id!r} already has a live candidate workspace"
                raise RuntimeContractError(message)
        candidate = FakeCandidateWorkspace(
            owner=self,
            config=_FakeCandidateConfig(
                workspace_id=workspace_id,
                path=self._root.path / workspace_id,
                revision=revision,
                trusted_input_baseline=self._root.trusted_input_baseline,
                revision_prefix=f"candidate-{len(self._candidates) + 1}",
            ),
        )
        for result, after_retention in self._candidate_retains:
            candidate.script_retain(result, after_retention=after_retention)
        self._candidate_retains.clear()
        self._candidates.append(candidate)
        return candidate

    async def create_measured_candidate(
        self, revision: str, *, step_tag: str | None = None
    ) -> CandidateWorkspace:
        """A candidate of *revision*; the fake declares no unread paths, so it withholds none."""
        candidate = await self.create_candidate(revision)
        self._measured.append(candidate)
        self._measured_step_tags.append(step_tag)
        return candidate

    @property
    def measured_step_tags(self) -> tuple[str | None, ...]:
        """The step tag each measured candidate was opened for, in order."""
        return tuple(self._measured_step_tags)

    @property
    def measured_candidates(self) -> tuple[CandidateWorkspace, ...]:
        """The candidates opened to evaluate (``create_measured_candidate``), in order."""
        return tuple(self._measured)

    async def adopt(
        self, revision: str, *, message: str = "vibesys: adopt revision"
    ) -> AdoptionPublication:
        """Publish a retained revision, without changing the root tree or index."""
        return self._root.publish(revision, message)

    async def update_checkout(self, publication: AdoptionPublication) -> CheckoutUpdate:
        """The Fake models its own run branch; pending edits refuse checkout changes."""
        if not self._root.has_publication(publication):
            message = "publication does not belong to this run"
            raise RuntimeContractError(message)
        if await self._root.pending_changes():
            return CheckoutUpdate(status=CheckoutStatus.DIRTY_TREE)
        if not self._root.can_fast_forward(publication):
            return CheckoutUpdate(status=CheckoutStatus.DIVERGED)
        self._root.apply_checkout(publication)
        return CheckoutUpdate(status=CheckoutStatus.FAST_FORWARDED)

    async def export_patch(self, revision: str) -> str:
        """Export one retained revision against the fake trusted baseline."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self.export_patch_calls.append(revision)
        source = self._root.tree_identity(revision) or revision
        default = self._default_patch or f"patch for {source}"
        return self._patches.get(revision, self._patches.get(source, default))

    async def retains(self, revision: str) -> bool:
        """Return whether the revision is retained, as opposed to merely present."""
        return self._root.retains(revision)

    def add_dangling_revision(self, revision: str) -> None:
        """Make a revision exportable and restorable yet not retained."""
        self._root.add_dangling_revision(revision)

    def set_patch(self, revision: str, patch: str) -> None:
        """Configure the canonical patch exported for a retained revision."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self._patches[revision] = patch

    def set_default_patch(self, patch: str) -> None:
        """Export ``patch`` for every revision without its own patch.

        Models snapshots that differ only in commits, not in content.
        """
        self._default_patch = patch

    def retain_candidate_revision(self, revision: str) -> None:
        """Keep a snapshotted candidate revision reachable from the root."""
        self._root.add_retained_revision(revision)

    def follow(self, holds: Callable[[], WorkspaceHolds]) -> None:
        """Read what core references from *holds* when deciding what a stop keeps."""
        self._holds = holds

    async def release_unheld(self) -> None:
        """Remove the worktrees a dead host left that core no longer references."""
        if self._holds is None:
            return
        kept = self._holds().workspace_ids()
        for leftover in self.leaked_candidates():
            if leftover.id not in kept:
                leftover.discard_leftover()

    def begin_close(self) -> None:
        """Reject new candidates before asynchronous teardown."""
        if self._closing:
            return
        self._closing = True

    async def close(self) -> None:
        """Discard candidates, once."""
        if self._closed:
            return
        self.begin_close()
        self._closed = True
        for candidate in reversed(self._candidates):
            await candidate.discard()


@dataclass(frozen=True, slots=True)
class FakeCommandCall:
    """One recorded sandboxed command request."""

    argv: tuple[str, ...]
    workspace: Workspace
    timeout_seconds: int | None
    output_argument: str | None = None


@dataclass(frozen=True, slots=True)
class FakeTrustedShellCall:
    """One recorded trusted shell recipe request."""

    command: str
    workspace: Workspace
    timeout_seconds: int | None


@dataclass(slots=True)
class FakeCommands:
    """Scriptable in-memory command execution with contract validation."""

    results: list[CommandResult | BaseException] = field(default_factory=list)
    calls: list[FakeCommandCall] = field(default_factory=list)
    trusted_shell_calls: list[FakeTrustedShellCall] = field(default_factory=list)

    def script(self, *results: CommandResult | BaseException) -> None:
        """Queue command results in invocation order."""
        self.results.extend(results)

    async def run(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one command, returning its scripted result."""
        validate_command(argv, timeout_seconds)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def capture_output(
        self,
        argv: tuple[str, ...],
        *,
        workspace: Workspace,
        output_argument: str,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Record one runtime-managed output capture and return its script."""
        validate_command(argv, timeout_seconds)
        validate_command((output_argument,), None)
        self.calls.append(FakeCommandCall(argv, workspace, timeout_seconds, output_argument))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)

    async def run_trusted_shell(
        self,
        command: str,
        *,
        workspace: Workspace,
        timeout_seconds: int | None = None,
    ) -> CommandResult:
        """Validate and record one audited shell recipe."""
        validate_trusted_shell_command(command, timeout_seconds)
        self.trusted_shell_calls.append(FakeTrustedShellCall(command, workspace, timeout_seconds))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return CommandResult(output="", exit_code=0)


class FakeWorkspace:
    """In-memory live workspace with revision and retention semantics."""

    def __init__(
        self,
        *,
        path: Path,
        workspace_id: str | None = None,
        revision: str | None = "fake-revision",
        trusted_input_baseline: str | None = None,
        known_revisions: set[str] | None = None,
    ) -> None:
        """Create a workspace at one recorded tree and immutable baseline."""
        self.access_recovery = WorkspaceAccessRecovery()
        self._path = path
        self._id = workspace_id
        self._revision = revision
        self._tree_revision = revision
        self._tree_aliases: dict[str, str] = {}
        self._publications: dict[str, AdoptionPublication] = {}
        self._publication_parents: dict[str, tuple[str, ...]] = {}
        self._trusted_input_baseline = (
            revision if trusted_input_baseline is None else trusted_input_baseline
        )
        self._known_revisions = set(known_revisions or ()) | {
            value for value in (revision, self._trusted_input_baseline) if value
        }
        self._dangling: set[str] = set()
        self._snapshot_count = 0
        # Revision names stay unique when a member-keyed candidate reuses an ID.
        self._revision_prefix = workspace_id or "fake"
        self._retained: dict[str, str] = {}
        self._retain_results: list[tuple[BaseException | None, bool]] = []
        self._pending_changes: list[list[str]] = []
        self._directories: set[str] = set()
        self.restore_calls: list[tuple[str, bool]] = []
        self._snapshot_labels: dict[str, str] = {}
        self._measured_inputs: dict[str, str] = {}
        self.agent_restore_calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def id(self) -> str | None:
        """Return the configured workspace identity."""
        return self._id

    @property
    def path(self) -> Path:
        """Return the configured host path without touching the filesystem."""
        return self._path

    @property
    def revision(self) -> str | None:
        """Return the latest recorded fake revision."""
        return self._revision

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the immutable configured trusted-input baseline."""
        return self._trusted_input_baseline

    @property
    def retained(self) -> dict[str, str]:
        """Return policy labels and revisions retained so far."""
        return dict(self._retained)

    async def snapshot(self, label: str) -> str:
        """Record a deterministic new revision for the current fake tree."""
        await self.access_recovery.reconcile(self)
        self._snapshot_count += 1
        revision = f"{self._revision_prefix}-revision-{self._snapshot_count}"
        self._snapshot_labels[label] = revision
        self._revision = revision
        self._tree_revision = revision
        self.add_retained_revision(revision)
        return revision

    def publish(self, revision: str, message: str) -> AdoptionPublication:
        """Keep publication history with the repository, across collection restarts."""
        if not self.retains(revision):
            detail = f"candidate revision is not retained by this run: {revision!r}"
            raise RuntimeContractError(detail)
        existing = self._publications.get(revision)
        if existing is not None:
            return existing
        commit = hashlib.sha1(
            f"{revision}:{message}:{len(self._publications)}".encode(),
            usedforsecurity=False,
        ).hexdigest()
        result = AdoptionPublication(
            revision=revision, commit=commit, branch=adoption_branch("fake-run")
        )
        parents = [self.revision]
        if self._publications:
            parents.append(tuple(self._publications.values())[-1].commit)
        self._publication_parents[commit] = tuple(
            parent for parent in parents if parent is not None
        )
        self._publications[revision] = result
        self.add_retained_revision(commit)
        self._tree_aliases[commit] = self.tree_identity(revision) or revision
        return result

    def has_publication(self, publication: AdoptionPublication) -> bool:
        """Whether this repository owns exactly this publication receipt."""
        return self._publications.get(publication.revision) == publication

    def can_fast_forward(self, publication: AdoptionPublication) -> bool:
        """Whether the current run head is in the publication's ancestry."""
        pending = [publication.commit]
        ancestors: set[str] = set()
        while pending:
            current = pending.pop()
            if current not in ancestors:
                ancestors.add(current)
                pending.extend(self._publication_parents.get(current, ()))
        return self._revision in ancestors

    def apply_checkout(self, publication: AdoptionPublication) -> None:
        """Advance the Fake's clean run checkout after a publication."""
        self._revision = publication.commit
        self._tree_revision = publication.commit
        self._tree_aliases[publication.commit] = (
            self.tree_identity(publication.revision) or publication.revision
        )

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a known tree while leaving recorded history unchanged."""
        self.restore_calls.append((revision, clean))
        if not self.knows_revision(revision):
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision

    def script_pending_changes(self, *changes: list[str]) -> None:
        """Queue workspace mutation observations in invocation order."""
        self._pending_changes.extend(changes)

    async def pending_changes(self) -> list[str]:
        """Return the next scripted uncommitted-change listing."""
        if self._pending_changes:
            return self._pending_changes.pop(0)
        return []

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        """Record an isolation restore while preserving only explicit grants."""
        if not self.knows_revision(revision):
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision
        self.agent_restore_calls.append((revision, preserve_paths))

    def declare_directories(self, *paths: str) -> None:
        """Declare which validated writable paths represent directories."""
        self._directories.update(paths)

    def is_directory(self, path: str) -> bool:
        """Return whether a writable grant covers descendants of *path*."""
        return path in self._directories

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Return whether a known tree could be materialized."""
        try:
            await self.restore(revision, clean=clean)
        except WorkspaceRestoreError:
            return False
        return True

    def script_retain(self, *results: BaseException | None, after_retention: bool = False) -> None:
        """Script errors before retention or after its durable acknowledgement boundary."""
        self._retain_results.extend((result, after_retention) for result in results)

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a known revision under a nonempty semantic label."""
        if not self.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        if not label:
            raise _WorkspaceRetentionLabelError
        error, after_retention = (
            self._retain_results.pop(0) if self._retain_results else (None, False)
        )
        if error is not None and not after_retention:
            raise error
        self._retained[label] = revision
        self.add_retained_revision(revision)
        if error is not None:
            raise error

    async def snapshot_and_retain(self, label: str, *, retention_label: str) -> str:
        """Record a revision and retain exactly that revision."""
        revision = await self.snapshot(label)
        await self.retain(revision, label=retention_label)
        return revision

    async def has_revision(self, revision: str) -> bool:
        """Return whether this fake can materialize a revision."""
        return self.knows_revision(revision)

    async def matches_revision(self, revision: str) -> bool:
        """Return whether the fake tree is exactly the revision's tree."""
        return self.tree_identity(self._tree_revision) == self.tree_identity(revision)

    def tree_identity(self, revision: str | None) -> str | None:
        """The shared tree identity of a source and every publication of it."""
        return None if revision is None else self._tree_aliases.get(revision, revision)

    async def find_snapshot(self, label: str) -> str | None:
        """Return the revision the snapshot with this label recorded."""
        return self._snapshot_labels.get(label)

    def script_measured_input(self, revision: str, digest: str) -> None:
        """Make *revision* read as the measured input *digest*, as an unread-only change does."""
        self._measured_inputs[revision] = digest

    async def measured_input(self, revision: str) -> str:
        """The scripted digest of *revision*, else one of its own."""
        source = self.tree_identity(revision) or revision
        return self._measured_inputs.get(
            revision, self._measured_inputs.get(source, f"input:{source}")
        )

    def knows_revision(self, revision: str) -> bool:
        """Return whether this fake can materialize a revision, retained or dangling."""
        return revision in self._known_revisions or revision in self._dangling

    def add_dangling_revision(self, revision: str) -> None:
        """Make a revision exist without being retained, like an unreferenced Git commit."""
        if revision not in self._known_revisions:
            self._dangling.add(revision)

    def retains(self, revision: str) -> bool:
        """Return whether a revision is held by this fake's retained history."""
        return revision in self._known_revisions

    @property
    def known_revisions(self) -> set[str]:
        """Return a copy of the revisions reachable from this fake workspace."""
        return set(self._known_revisions)

    def add_retained_revision(self, revision: str) -> None:
        """Make an externally retained revision materializable."""
        self._known_revisions.add(revision)


class FakeCandidateWorkspace(FakeWorkspace):
    """Faithful isolated fake with explicit, idempotent lifetime."""

    def __init__(
        self,
        *,
        owner: FakeWorkspaces,
        config: _FakeCandidateConfig,
    ) -> None:
        """Bind a candidate to the one fake run that created it."""
        super().__init__(
            workspace_id=config.workspace_id,
            path=config.path,
            revision=config.revision,
            trusted_input_baseline=config.trusted_input_baseline,
        )
        self._owner = owner
        self._discarded = False
        self.orphaned = False
        self._revision_prefix = config.revision_prefix

    def tree_identity(self, revision: str | None) -> str | None:
        """Resolve publication trees through this run's shared repository."""
        return self._owner.root.tree_identity(revision)

    def knows_revision(self, revision: str) -> bool:
        """Resolve revisions through the owning run's shared repository."""
        return self._owner.root.knows_revision(revision)

    @property
    def known_revisions(self) -> set[str]:
        """Return revisions available from the owning run's shared repository."""
        return self._owner.root.known_revisions

    def add_retained_revision(self, revision: str) -> None:
        """Publish an available revision to the owning run's shared repository."""
        self._owner.retain_candidate_revision(revision)

    @property
    def discarded(self) -> bool:
        """Return whether the isolated workspace has been released."""
        return self._discarded

    def discard_leftover(self) -> None:
        """Remove the worktree a dead host left."""
        self._discarded = True

    @property
    def trusted_input_baseline(self) -> str | None:
        """Return the immutable baseline while its resources are live."""
        self._require_open()
        return super().trusted_input_baseline

    @property
    def path(self) -> Path:
        """Return the isolated path while its resources are live."""
        self._require_open()
        return super().path

    @property
    def revision(self) -> str | None:
        """Return the materialized candidate revision while resources are live."""
        self._require_open()
        return self._tree_revision

    async def snapshot(self, label: str) -> str:
        """Record a candidate revision while the workspace is live."""
        self._require_open()
        return await super().snapshot(label)

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Restore a candidate revision while the workspace is live."""
        self._require_open()
        await super().restore(revision, clean=clean)

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Try to restore a candidate revision while the workspace is live."""
        self._require_open()
        return await super().try_restore(revision, clean=clean)

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a candidate revision while the workspace is live."""
        self._require_open()
        await super().retain(revision, label=label)

    async def pending_changes(self) -> list[str]:
        """List changes only while the isolated resources are live."""
        self._require_open()
        return await super().pending_changes()

    async def discard(self) -> None:
        """Release this fake candidate idempotently."""
        if self._discarded:
            return
        self._discarded = True

    def _require_open(self) -> None:
        """Reject operations whose isolated resources no longer exist."""
        if self._discarded:
            message = "candidate workspace is closed"
            raise RuntimeContractError(message)


@dataclass(frozen=True, slots=True)
class FakeAccuracyCall:
    """One recorded accuracy evaluation request."""

    workspace: Workspace
    reuse: AccuracyReceipt | None


@dataclass(frozen=True, slots=True)
class FakeBenchmarkCall:
    """One recorded benchmark evaluation request."""

    workspace: Workspace
    objectives: tuple[BenchmarkObjective, ...]


@dataclass(frozen=True, slots=True)
class FakeLocalValidationCall:
    """One recorded candidate-authored local validation request."""

    workspace: Workspace
    recipe_artifact: str
    report_location: str


FakeEvaluationKind: TypeAlias = Literal["build", "accuracy", "benchmark"]


class FakeEvaluationGate:
    """Holds one trusted evaluation call until the test releases it.

    The gated call sets :attr:`entered` when it starts and then waits for
    :meth:`release`, so a test can order events across concurrent work without
    yielding a counted number of times or reading a clock.
    """

    def __init__(self) -> None:
        """Create an unreleased gate."""
        self.entered = asyncio.Event()
        self._release = asyncio.Event()
        self.cancelled_while_live = False
        self.finished = False

    @property
    def released(self) -> bool:
        """Return whether the test has released the held call."""
        return self._release.is_set()

    def release(self) -> None:
        """Let the held call continue to its scripted outcome."""
        self._release.set()

    async def hold(self, workspace: Workspace) -> None:
        """Wait for release, noting a cancellation that arrives while ``workspace`` is live."""
        self.entered.set()
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            discarded = isinstance(workspace, FakeCandidateWorkspace) and workspace.discarded
            self.cancelled_while_live = not discarded
            raise
        finally:
            self.finished = True


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects.

    Scripted results are consumed in call order; a scripted exception is raised
    by the call that consumes it. Benchmarks of the root workspace (the run's
    input, whose ``id`` is ``None``) consume :attr:`root_benchmark_results`
    first, so a test can script the input and candidates independently of how
    their calls interleave. A gate holds the n-th call of one kind (counted
    from zero across all workspaces) until the test releases it. What
    :attr:`printed` scripts for a kind is written to each call's ``output``
    before its gate holds it, as a running command prints before it ends.
    """

    default_build: BuildEvaluation = field(default_factory=lambda: BuildEvaluation(executed=False))
    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    default_local_validation: LocalValidationEvaluation = field(
        default_factory=lambda: LocalValidationEvaluation(passed=True)
    )
    build_results: list[BuildEvaluation | BaseException] = field(default_factory=list)
    build_calls: list[Workspace] = field(default_factory=list)
    accuracy_results: list[AccuracyEvaluation | BaseException] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation | BaseException] = field(default_factory=list)
    root_benchmark_results: list[BenchmarkEvaluation | BaseException] = field(default_factory=list)
    local_validation_results: list[LocalValidationEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    local_validation_calls: list[FakeLocalValidationCall] = field(default_factory=list)
    run_id: str = "test-run"
    printed: dict[FakeEvaluationKind, tuple[bytes, bytes]] = field(default_factory=dict)
    """The (stdout, stderr) each accuracy or benchmark call of a kind prints."""
    _gates: dict[tuple[FakeEvaluationKind, int], FakeEvaluationGate] = field(default_factory=dict)

    def script_build(self, *results: BuildEvaluation | BaseException) -> None:
        """Queue build results or failures in call order."""
        self.build_results.extend(results)

    def script_accuracy(self, *results: AccuracyEvaluation | BaseException) -> None:
        """Queue accuracy results or failures in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation | BaseException) -> None:
        """Queue benchmark results or failures in call order."""
        self.benchmark_results.extend(results)

    def script_root_benchmark(self, *results: BenchmarkEvaluation | BaseException) -> None:
        """Queue results or failures for benchmarks of the root workspace only."""
        self.root_benchmark_results.extend(results)

    def script_local_validation(self, *results: LocalValidationEvaluation) -> None:
        """Queue local-validation results in call order."""
        self.local_validation_results.extend(results)

    def gate(self, kind: FakeEvaluationKind, call: int) -> FakeEvaluationGate:
        """Hold the ``call``-th evaluation of ``kind`` (from zero) until released."""
        key = (kind, call)
        if key in self._gates:
            message = f"{kind} call {call} is already gated"
            raise ValueError(message)
        gate = FakeEvaluationGate()
        self._gates[key] = gate
        return gate

    async def _pass_gate(
        self,
        kind: FakeEvaluationKind,
        call: int,
        workspace: Workspace,
        output: OutputSink,
    ) -> None:
        stdout, stderr = self.printed.get(kind, (b"", b""))
        output.write(OutputStream.STDOUT, stdout)
        output.write(OutputStream.STDERR, stderr)
        gate = self._gates.get((kind, call))
        if gate is not None:
            await gate.hold(workspace)

    async def build(self, workspace: Workspace, *, output: OutputSink) -> BuildEvaluation:
        """Record the request, print the scripted output, and return the next scripted result."""
        self.build_calls.append(workspace)
        await self._pass_gate("build", len(self.build_calls) - 1, workspace, output)
        result = self.build_results.pop(0) if self.build_results else self.default_build
        if isinstance(result, BaseException):
            raise result
        return result

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
        output: OutputSink,
    ) -> AccuracyEvaluation:
        """Record the request, print the scripted output, and return the next scripted result."""
        self.accuracy_calls.append(FakeAccuracyCall(workspace, reuse))
        # A reused pass runs no command, so it prints nothing.
        printing = output if reuse is None else DISCARD
        await self._pass_gate("accuracy", len(self.accuracy_calls) - 1, workspace, printing)
        if reuse is not None:
            if reuse.run_id != self.run_id:
                message = "accuracy receipt belongs to another run"
                raise RuntimeContractError(message)
            if reuse.workspace_id != workspace.id:
                message = "accuracy receipt belongs to another workspace"
                raise RuntimeContractError(message)
            if reuse.revision != workspace.revision:
                message = "accuracy receipt does not match the current workspace revision"
                raise RuntimeContractError(message)
            return AccuracyEvaluation(executed=False, receipt=reuse)
        result = self.accuracy_results.pop(0) if self.accuracy_results else self.default_accuracy
        if isinstance(result, BaseException):
            raise result
        if not result.passed:
            return result
        revision = workspace.revision
        if revision is None:
            message = "accuracy requires a recorded workspace revision"
            raise RuntimeContractError(message)
        receipt = result.receipt or AccuracyReceipt(
            run_id=self.run_id,
            workspace_id=workspace.id,
            revision=revision,
        )
        return result.model_copy(update={"receipt": receipt})

    async def benchmark(
        self,
        workspace: Workspace,
        *,
        objectives: tuple[BenchmarkObjective, ...] = (),
        output: OutputSink,
    ) -> BenchmarkEvaluation:
        """Record the request, print the scripted output, and return the next scripted result."""
        validate_objectives(objectives)
        self.benchmark_calls.append(FakeBenchmarkCall(workspace, objectives))
        await self._pass_gate("benchmark", len(self.benchmark_calls) - 1, workspace, output)
        if workspace.id is None and self.root_benchmark_results:
            result = self.root_benchmark_results.pop(0)
        elif self.benchmark_results:
            result = self.benchmark_results.pop(0)
        else:
            result = self.default_benchmark
        if isinstance(result, BaseException):
            raise result
        return result

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Record one semantic local-validation request and return its script.

        Like the product evaluation, an unusable recipe reference is a failed
        outcome the agent can correct, while a bad report location is a contract error.
        """
        validate_workspace_writable_paths(WorkspaceAccess.LIMITED, (report_location,))
        try:
            check_recipe_artifact_path(recipe_artifact)
        except LocalValidationRecipeError as error:
            return LocalValidationEvaluation(
                passed=False, feedback=str(error), recipe_unusable=True
            )
        self.local_validation_calls.append(
            FakeLocalValidationCall(workspace, recipe_artifact, report_location)
        )
        if self.local_validation_results:
            return self.local_validation_results.pop(0)
        return self.default_local_validation


class FakeStopTimer:
    """A stop timer whose waits end only when a test expires them.

    Production times a stop's grace period with ``asyncio.sleep``; this Fake
    records each requested delay and lets the test decide when it has passed,
    from any thread, so no test waits on the wall clock.
    """

    def __init__(self, threads: Threads) -> None:
        """Start with no recorded or pending waits; ``threads`` makes the lock."""
        self.delays: list[float] = []
        self._lock = threads.lock()
        self._pending: list[tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]] = []

    async def __call__(self, seconds: float) -> None:
        """Record *seconds* and wait until :meth:`expire`."""
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future[None] = loop.create_future()
        entry = (loop, waiter)
        with self._lock:
            self.delays.append(seconds)
            self._pending.append(entry)
        try:
            await waiter
        finally:
            with self._lock:
                if entry in self._pending:
                    self._pending.remove(entry)

    def expire(self) -> None:
        """End every pending wait, as if its delay had elapsed."""
        with self._lock:
            pending, self._pending = self._pending, []
        for loop, waiter in pending:
            loop.call_soon_threadsafe(_resolve, waiter)


def _resolve(waiter: asyncio.Future[None]) -> None:
    if not waiter.done():
        waiter.set_result(None)
