"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TypeAlias, TypeVar, overload

from pydantic import BaseModel

from vs_runtime.contracts import (
    AccuracyEvaluation,
    AccuracyReceipt,
    AgentBinding,
    AgentRole,
    AgentSession,
    BenchmarkEvaluation,
    BenchmarkObjective,
    CandidateWorkspace,
    CommandResult,
    LocalValidationEvaluation,
    OrchestrationPlugin,
    ResolvedSkillResources,
    RunFacts,
    RuntimeContractError,
    SessionClosedError,
    SkillCatalogError,
    SkillResolution,
    SkillResourceRequest,
    StateModelError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceAccess,
    WorkspaceRestoreError,
    validate_command,
    validate_member_id,
    validate_objectives,
    validate_trusted_shell_command,
    validate_workspace_writable_paths,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


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
class _FakeSessionConfig:
    """Immutable creation options shared by a fake session."""

    member_id: str | None
    writable_paths: tuple[str, ...]


@dataclass(frozen=True)
class _FakeCandidateConfig:
    """Creation state copied into one isolated fake workspace."""

    workspace_id: str
    path: Path
    revision: str
    trusted_input_baseline: str | None
    known_revisions: set[str]


def _echo_responder(
    _role: AgentRole,
    _history: tuple[str, ...],
    message: str,
    _response: type[BaseModel] | None,
) -> object:
    return message


class FakeAgentSession:
    """In-memory conversation with the public lifetime and context contract."""

    def __init__(
        self,
        role: AgentRole,
        workspace: Workspace,
        binding: AgentBinding,
        responder: TurnResponder,
        config: _FakeSessionConfig,
    ) -> None:
        """Bind one fresh session to immutable creation configuration."""
        self._role = role
        self._workspace = workspace
        self._member_id = config.member_id
        self._writable_paths = config.writable_paths
        self._binding = binding
        self._responder = responder
        self._history: list[str] = []
        self._turn_number = 0
        self._closed = False

    @property
    def role(self) -> AgentRole:
        """Return the role bound at creation."""
        return self._role

    @property
    def workspace(self) -> Workspace:
        """Return the workspace bound at creation."""
        return self._workspace

    @property
    def member_id(self) -> str | None:
        """Return the durable policy identity for this instance."""
        return self._member_id

    @property
    def writable_paths(self) -> tuple[str, ...]:
        """Return the immutable session-specific write grants."""
        return self._writable_paths

    @property
    def binding(self) -> AgentBinding:
        """Return the configured immutable runtime attribution."""
        return self._binding

    @property
    def closed(self) -> bool:
        """Return whether cleanup has ended this session."""
        return self._closed

    @property
    def history(self) -> tuple[str, ...]:
        """Return completed user messages in conversation order."""
        return tuple(self._history)

    @overload
    async def turn(self, message: str, *, response: None = None) -> str: ...

    @overload
    async def turn(self, message: str, *, response: type[ResponseT]) -> ResponseT: ...

    async def turn(
        self, message: str, *, response: type[ResponseT] | None = None
    ) -> str | ResponseT:
        """Respond from prior completed turns, then append this message."""
        if self._closed:
            raise SessionClosedError
        self._turn_number += 1
        label = f"{self._role.id}-session-turn-{self._turn_number}"
        await self._workspace.snapshot(f"{label}-input")
        value = self._responder(self._role, tuple(self._history), message, response)
        if response is None:
            if not isinstance(value, str):
                message = "text turn responder must return str"
                raise TypeError(message)
            result: str | ResponseT = value
        else:
            result = response.model_validate(value)
        self._history.append(message)
        if self._role.workspace_access is WorkspaceAccess.READ_WRITE:
            await self._workspace.snapshot(label)
        return result

    async def close(self) -> None:
        """End the fake conversation idempotently."""
        self._closed = True


class FakeAgentSessions:
    """Run-owned in-memory factory with isolated creation semantics."""

    def __init__(
        self,
        agents: tuple[AgentRole, ...],
        *,
        responder: TurnResponder = _echo_responder,
        bindings: dict[str, AgentBinding] | None = None,
    ) -> None:
        """Build the private role lookup from the plugin's authoritative tuple."""
        self._roles = {role.id: role for role in agents}
        self._responder = responder
        self._bindings = bindings or {
            role.id: AgentBinding(backend="fake", driver="fake", provider="fake") for role in agents
        }
        self._sessions: list[FakeAgentSession] = []
        self._creation_results: list[BaseException | None] = []
        self._closed = False

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

    def script_creation(self, *results: BaseException | None) -> None:
        """Queue deterministic session-creation successes or failures."""
        self._creation_results.extend(results)

    async def create_session(
        self,
        role: AgentRole,
        *,
        workspace: Workspace,
        member_id: str | None = None,
        writable_paths: tuple[str, ...] = (),
    ) -> AgentSession:
        """Validate the declared role and create an independent conversation."""
        if self._closed:
            raise SessionClosedError
        if self._creation_results:
            failure = self._creation_results.pop(0)
            if failure is not None:
                raise failure
        if self._roles.get(role.id) != role:
            raise UnknownAgentRoleError(role.id)
        validate_member_id(member_id)
        validated_paths = validate_workspace_writable_paths(
            role.workspace_access,
            writable_paths,
        )
        session = FakeAgentSession(
            role,
            workspace,
            self._bindings[role.id],
            self._responder,
            _FakeSessionConfig(member_id, validated_paths),
        )
        self._sessions.append(session)
        return session

    async def close(self) -> None:
        """Close all sessions in reverse creation order, once."""
        if self._closed:
            return
        self._closed = True
        for session in reversed(self._sessions):
            await session.close()

    async def close_workspace_sessions(self, workspace: Workspace) -> None:
        """Invalidate every public session bound to a discarded workspace."""
        for session in self._sessions:
            if session.workspace is workspace:
                await session.close()


class FakeWorkspaces:
    """In-memory owner of one root and its isolated candidate workspaces."""

    def __init__(
        self,
        root: FakeWorkspace,
        *,
        supports_parallel_candidates: bool = False,
        invalidate_sessions: Callable[[Workspace], Awaitable[None]] | None = None,
    ) -> None:
        """Bind the fake capability to one root and a fixed isolation capability."""
        self._root = root
        self._supports_parallel_candidates = supports_parallel_candidates
        self._invalidate_sessions = invalidate_sessions
        self._candidates: list[FakeCandidateWorkspace] = []
        self._patches: dict[str, str] = {}
        self.export_patch_calls: list[str] = []
        self._closed = False

    @property
    def root(self) -> FakeWorkspace:
        """Return the configured fake root workspace."""
        return self._root

    @property
    def supports_parallel_candidates(self) -> bool:
        """Return the fixed candidate-isolation capability."""
        return self._supports_parallel_candidates

    @property
    def candidates(self) -> tuple[FakeCandidateWorkspace, ...]:
        """Return candidates in creation order, including discarded ones."""
        return tuple(self._candidates)

    async def create_candidate(self, from_revision: str | None = None) -> CandidateWorkspace:
        """Create one isolated workspace from a known root revision."""
        if self._closed:
            raise SessionClosedError
        if not self._supports_parallel_candidates:
            message = "this run does not support parallel candidate workspaces"
            raise RuntimeContractError(message)
        revision = from_revision or self._root.revision
        if revision is None or not self._root.knows_revision(revision):
            raise WorkspaceRestoreError(from_revision or "")
        workspace_id = f"candidate-{len(self._candidates) + 1}"
        candidate = FakeCandidateWorkspace(
            owner=self,
            invalidate_sessions=self._invalidate_sessions,
            config=_FakeCandidateConfig(
                workspace_id=workspace_id,
                path=self._root.path / workspace_id,
                revision=revision,
                trusted_input_baseline=self._root.trusted_input_baseline,
                known_revisions=self._root.known_revisions,
            ),
        )
        self._candidates.append(candidate)
        return candidate

    async def adopt(self, revision: str) -> None:
        """Adopt a retained revision even after its candidate was discarded."""
        if not self._root.knows_revision(revision):
            message = f"candidate revision is not retained by this run: {revision!r}"
            raise RuntimeContractError(message)
        await self._root.restore(revision)

    async def export_patch(self, revision: str) -> str:
        """Export one retained revision against the fake trusted baseline."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self.export_patch_calls.append(revision)
        return self._patches.get(revision, f"patch for {revision}")

    def set_patch(self, revision: str, patch: str) -> None:
        """Configure the canonical patch exported for a retained revision."""
        if not self._root.knows_revision(revision):
            raise _UnknownWorkspaceRevisionError(revision)
        self._patches[revision] = patch

    def retain_candidate_revision(self, revision: str) -> None:
        """Keep a snapshotted candidate revision reachable from the root."""
        self._root.add_retained_revision(revision)

    async def close(self) -> None:
        """Discard every live candidate in reverse creation order, once."""
        if self._closed:
            return
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


@dataclass(slots=True)
class FakeSkills:
    """In-memory skill catalog with the production resolver's observable rules."""

    installed_resources: dict[str, tuple[str, ...]] = field(default_factory=dict)
    catalog_error: str | None = None

    async def resolve(self, requests: tuple[SkillResourceRequest, ...]) -> SkillResolution:
        """Resolve requests against installed names and their available files."""
        if not requests:
            return SkillResolution()
        if self.catalog_error is not None:
            raise SkillCatalogError(self.catalog_error)
        if not self.installed_resources:
            return SkillResolution(diagnostics=("no skill sources are installed",))

        return _resolve_fake_skills(requests, self.installed_resources)


def _resolve_fake_skills(
    requests: tuple[SkillResourceRequest, ...], installed: dict[str, tuple[str, ...]]
) -> SkillResolution:
    """Mirror production name merging and partial resource validation in memory."""
    merged: dict[str, tuple[str, list[str]]] = {}
    diagnostics: list[str] = []
    for index, request in enumerate(requests, start=1):
        name = request.name.strip()
        if name not in installed:
            diagnostics.append(f"selection #{index}: unknown installed skill {name!r}")
            continue
        if name not in merged:
            merged[name] = (request.purpose.strip(), [])
        purpose, resources = merged[name]
        diagnostics.extend(
            _resolve_fake_resources(
                request.resource_paths,
                name=name,
                selection_index=index,
                available=set(installed[name]),
                resources=resources,
            )
        )
        merged[name] = purpose, resources

    return SkillResolution(
        resolved=tuple(
            ResolvedSkillResources(
                name=name,
                router_path=f"{name}/SKILL.md",
                resource_paths=tuple(f"{name}/{path}" for path in resources),
                purpose=purpose,
            )
            for name, (purpose, resources) in merged.items()
        ),
        diagnostics=tuple(diagnostics),
    )


def _resolve_fake_resources(
    paths: tuple[str, ...],
    *,
    name: str,
    selection_index: int,
    available: set[str],
    resources: list[str],
) -> list[str]:
    """Validate one request's paths using the catalog's in-memory file set."""
    diagnostics: list[str] = []
    for raw_path in paths:
        resource = raw_path.strip()
        path = PurePosixPath(resource)
        if (
            not resource
            or "\\" in resource
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
        ):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path must be relative and stay within the skill"
            )
            continue
        if any(part in {".git", "repos", "__pycache__"} for part in path.parts):
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource path is excluded from agent skill materialization"
            )
            continue
        resource = path.as_posix()
        if resource not in available:
            diagnostics.append(
                f"selection #{selection_index} skill {name!r} resource {raw_path!r}: "
                "resource file does not exist"
            )
            continue
        if resource != "SKILL.md" and resource not in resources:
            resources.append(resource)
    return diagnostics


class FakeWorkspace:
    """In-memory live workspace with revision and retention semantics."""

    def __init__(
        self,
        *,
        path: Path = Path(),
        workspace_id: str | None = None,
        revision: str | None = "fake-revision",
        trusted_input_baseline: str | None = None,
        known_revisions: set[str] | None = None,
    ) -> None:
        """Create a workspace at one recorded tree and immutable baseline."""
        self._path = path
        self._id = workspace_id
        self._revision = revision
        self._tree_revision = revision
        self._trusted_input_baseline = (
            revision if trusted_input_baseline is None else trusted_input_baseline
        )
        self._known_revisions = set(known_revisions or ()) | {
            value for value in (revision, self._trusted_input_baseline) if value
        }
        self._snapshot_count = 0
        self._retained: dict[str, str] = {}
        self._pending_changes: list[list[str]] = []
        self.restore_calls: list[tuple[str, bool]] = []

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
        del label
        self._snapshot_count += 1
        prefix = self._id or "fake"
        revision = f"{prefix}-revision-{self._snapshot_count}"
        self._revision = revision
        self._tree_revision = revision
        self._known_revisions.add(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a known tree while leaving recorded history unchanged."""
        self.restore_calls.append((revision, clean))
        if revision not in self._known_revisions:
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

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        """Return whether a known tree could be materialized."""
        try:
            await self.restore(revision, clean=clean)
        except WorkspaceRestoreError:
            return False
        return True

    async def retain(self, revision: str, *, label: str) -> None:
        """Retain a known revision under a nonempty semantic label."""
        if revision not in self._known_revisions:
            raise _UnknownWorkspaceRevisionError(revision)
        if not label:
            raise _WorkspaceRetentionLabelError
        self._retained[label] = revision

    def knows_revision(self, revision: str) -> bool:
        """Return whether this fake can materialize a revision."""
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
        invalidate_sessions: Callable[[Workspace], Awaitable[None]] | None,
        config: _FakeCandidateConfig,
    ) -> None:
        """Bind a candidate to the one fake run that created it."""
        super().__init__(
            workspace_id=config.workspace_id,
            path=config.path,
            revision=config.revision,
            trusted_input_baseline=config.trusted_input_baseline,
            known_revisions=config.known_revisions,
        )
        self._owner = owner
        self._invalidate_sessions = invalidate_sessions
        self._discarded = False

    @property
    def discarded(self) -> bool:
        """Return whether the isolated workspace has been released."""
        return self._discarded

    @property
    def path(self) -> Path:
        """Return the isolated path while its resources are live."""
        self._require_open()
        return super().path

    @property
    def revision(self) -> str | None:
        """Return the recorded candidate revision while resources are live."""
        self._require_open()
        return super().revision

    async def snapshot(self, label: str) -> str:
        """Record a candidate revision while the workspace is live."""
        self._require_open()
        revision = await super().snapshot(label)
        self._owner.retain_candidate_revision(revision)
        return revision

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

    async def discard(self) -> None:
        """Release this fake candidate idempotently."""
        if self._discarded:
            return
        if self._invalidate_sessions is not None:
            await self._invalidate_sessions(self)
        self._discarded = True

    def _require_open(self) -> None:
        """Reject operations whose isolated resources no longer exist."""
        if self._discarded:
            message = "candidate workspace is closed"
            raise RuntimeContractError(message)


class FakeControl:
    """Deterministic cooperative-control boundary for plugin tests."""

    def __init__(self) -> None:
        """Create an active control with no pending failure."""
        self._failure: BaseException | None = None
        self._checkpoints = 0

    @property
    def checkpoints(self) -> int:
        """Return how many checkpoints plugin control flow reached."""
        return self._checkpoints

    def fail_with(self, error: BaseException | None) -> None:
        """Configure the error raised at subsequent checkpoints, or clear it."""
        self._failure = error

    async def checkpoint(self) -> None:
        """Record the boundary and raise its configured stop failure, if any."""
        self._checkpoints += 1
        if self._failure is not None:
            raise self._failure


@dataclass(frozen=True, slots=True)
class FakeStateCommit:
    """One validated state replacement recorded by :class:`FakeState`."""

    value: BaseModel
    workspace: Workspace | None
    label: str | None


class FakeState:
    """Faithful in-memory plugin-state durability with detached values."""

    def __init__(self, model: type[BaseModel] | None, root: Workspace) -> None:
        """Bind the exact plugin declaration and its only live workspace."""
        self._model = model
        self._root = root
        self._value: BaseModel | None = None
        self._commits: list[FakeStateCommit] = []

    @property
    def commits(self) -> tuple[FakeStateCommit, ...]:
        """Return detached commit records in durability order."""
        return tuple(
            FakeStateCommit(
                type(commit.value).model_validate_json(
                    commit.value.model_dump_json(round_trip=True)
                ),
                commit.workspace,
                commit.label,
            )
            for commit in self._commits
        )

    async def load(self, model: type[ResponseT]) -> ResponseT | None:
        """Return a detached value after validating the exact declared model."""
        self._require_model(model)
        if self._value is None:
            return None
        return model.model_validate_json(self._value.model_dump_json(round_trip=True))

    async def commit(
        self,
        value: BaseModel,
        *,
        workspace: Workspace | None = None,
        label: str | None = None,
    ) -> None:
        """Record one deep-validated replacement and optional root association."""
        self._require_model(type(value))
        if workspace is not None and workspace is not self._root:
            message = "state can commit only the live root workspace for this run"
            raise RuntimeContractError(message)
        model = self._model
        if model is None:
            raise StateModelError(None, type(value))
        snapshot = model.model_validate_json(value.model_dump_json(round_trip=True))
        self._value = snapshot
        recorded = model.model_validate_json(snapshot.model_dump_json(round_trip=True))
        self._commits.append(FakeStateCommit(recorded, workspace, label))

    def _require_model(self, model: type[BaseModel]) -> None:
        if model is not self._model:
            raise StateModelError(self._model, model)


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


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects."""

    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    default_local_validation: LocalValidationEvaluation = field(
        default_factory=lambda: LocalValidationEvaluation(passed=True)
    )
    accuracy_results: list[AccuracyEvaluation] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation] = field(default_factory=list)
    local_validation_results: list[LocalValidationEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    local_validation_calls: list[FakeLocalValidationCall] = field(default_factory=list)
    run_id: str = "test-run"

    def script_accuracy(self, *results: AccuracyEvaluation) -> None:
        """Queue accuracy results in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation) -> None:
        """Queue benchmark results in call order."""
        self.benchmark_results.extend(results)

    def script_local_validation(self, *results: LocalValidationEvaluation) -> None:
        """Queue local-validation results in call order."""
        self.local_validation_results.extend(results)

    async def accuracy(
        self,
        workspace: Workspace,
        *,
        reuse: AccuracyReceipt | None = None,
    ) -> AccuracyEvaluation:
        """Record the request and return the next scripted result."""
        self.accuracy_calls.append(FakeAccuracyCall(workspace, reuse))
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
    ) -> BenchmarkEvaluation:
        """Record the request and return the next scripted result."""
        validate_objectives(objectives)
        self.benchmark_calls.append(FakeBenchmarkCall(workspace, objectives))
        if self.benchmark_results:
            return self.benchmark_results.pop(0)
        return self.default_benchmark

    async def validate_local(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
    ) -> LocalValidationEvaluation:
        """Record one semantic local-validation request and return its script."""
        validate_workspace_writable_paths(
            WorkspaceAccess.LIMITED,
            (recipe_artifact, report_location),
        )
        self.local_validation_calls.append(
            FakeLocalValidationCall(workspace, recipe_artifact, report_location)
        )
        if self.local_validation_results:
            return self.local_validation_results.pop(0)
        return self.default_local_validation


class FakeRunHost:
    """In-memory run host that owns fake sessions and captured log lines."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-040117 [PLR0913]; these are independent fake inputs, and a fake-host options bundle would add a second public configuration shape solely to shorten this signature.
        self,
        plugin: OrchestrationPlugin,
        *,
        run_id: str = "test-run",
        project_root: Path = Path(),
        facts: RunFacts | None = None,
        responder: TurnResponder = _echo_responder,
        agent_bindings: dict[str, AgentBinding] | None = None,
        supports_parallel_candidates: bool = False,
    ) -> None:
        """Create a host whose private role map derives from ``plugin.agents``."""
        self._run_id = run_id
        self._facts = (
            RunFacts(domain_id="generic", objective="Test objective.") if facts is None else facts
        )
        self._agents = FakeAgentSessions(
            plugin.agents,
            responder=responder,
            bindings=agent_bindings,
        )
        self._workspaces = FakeWorkspaces(
            FakeWorkspace(path=project_root),
            supports_parallel_candidates=supports_parallel_candidates,
            invalidate_sessions=self._agents.close_workspace_sessions,
        )
        self._evaluation = FakeEvaluation(run_id=run_id)
        self._state = FakeState(plugin.state, self._workspaces.root)
        self._control = FakeControl()
        self._commands = FakeCommands()
        self._skills = FakeSkills()
        self._logs: list[str] = []
        self._closed = False

    @property
    def run_id(self) -> str:
        """Return this fake run's stable identity."""
        return self._run_id

    @property
    def facts(self) -> RunFacts:
        """Return the configured immutable run facts."""
        return self._facts

    @property
    def workspaces(self) -> FakeWorkspaces:
        """Return the fake live-workspace capability."""
        return self._workspaces

    @property
    def agents(self) -> FakeAgentSessions:
        """Return the run-owned fake session factory."""
        return self._agents

    @property
    def evaluation(self) -> FakeEvaluation:
        """Return the scriptable trusted evaluation capability."""
        return self._evaluation

    @property
    def state(self) -> FakeState:
        """Return plugin-bound in-memory state durability."""
        return self._state

    @property
    def control(self) -> FakeControl:
        """Return the deterministic cooperative-control capability."""
        return self._control

    @property
    def commands(self) -> FakeCommands:
        """Return deterministic sandboxed command execution."""
        return self._commands

    @property
    def skills(self) -> FakeSkills:
        """Return deterministic installed-skill resolution."""
        return self._skills

    @property
    def logs(self) -> tuple[str, ...]:
        """Return recorded log messages in call order."""
        return tuple(self._logs)

    def log(self, message: str) -> None:
        """Capture one log message."""
        self._logs.append(message)

    async def close(self) -> None:
        """Close all run-owned resources idempotently."""
        if self._closed:
            return
        self._closed = True
        await self._agents.close()
        await self._workspaces.close()
