"""Faithful in-memory implementations of the public runtime contracts."""

from __future__ import annotations

from collections.abc import Callable
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
    CommandResult,
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
    WorkspaceRestoreError,
    validate_command,
    validate_member_id,
    validate_objectives,
    validate_workspace_writable_paths,
)

ResponseT = TypeVar("ResponseT", bound=BaseModel)
TurnResponder: TypeAlias = Callable[
    [AgentRole, tuple[str, ...], str, type[BaseModel] | None], object
]


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
        value = self._responder(self._role, tuple(self._history), message, response)
        if response is None:
            if not isinstance(value, str):
                message = "text turn responder must return str"
                raise TypeError(message)
            result: str | ResponseT = value
        else:
            result = response.model_validate(value)
        self._history.append(message)
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
        self._closed = False

    @property
    def sessions(self) -> tuple[FakeAgentSession, ...]:
        """Return created sessions in ownership order."""
        return tuple(self._sessions)

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


class FakeWorkspaces:
    """In-memory holder for one fake root workspace."""

    def __init__(self, root: Workspace) -> None:
        """Bind the fake capability to one root workspace."""
        self._root = root

    @property
    def root(self) -> Workspace:
        """Return the configured fake root workspace."""
        return self._root


@dataclass(frozen=True, slots=True)
class FakeCommandCall:
    """One recorded sandboxed command request."""

    argv: tuple[str, ...]
    workspace: Workspace
    timeout_seconds: int | None


@dataclass(slots=True)
class FakeCommands:
    """Scriptable in-memory command execution with contract validation."""

    results: list[CommandResult] = field(default_factory=list)
    calls: list[FakeCommandCall] = field(default_factory=list)

    def script(self, *results: CommandResult) -> None:
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
            return self.results.pop(0)
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
    ) -> None:
        """Create a workspace at one recorded tree and immutable baseline."""
        self._path = path
        self._id = workspace_id
        self._revision = revision
        self._tree_revision = revision
        self._trusted_input_baseline = (
            revision if trusted_input_baseline is None else trusted_input_baseline
        )
        self._known_revisions = {
            value for value in (revision, self._trusted_input_baseline) if value
        }
        self._snapshot_count = 0
        self._retained: dict[str, str] = {}

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
        revision = f"fake-revision-{self._snapshot_count}"
        self._revision = revision
        self._tree_revision = revision
        self._known_revisions.add(revision)
        return revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        """Materialize a known tree while leaving recorded history unchanged."""
        del clean
        if revision not in self._known_revisions:
            raise WorkspaceRestoreError(revision)
        self._tree_revision = revision

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


@dataclass(slots=True)
class FakeEvaluation:
    """Scriptable in-memory implementation of trusted evaluation effects."""

    default_accuracy: AccuracyEvaluation = field(
        default_factory=lambda: AccuracyEvaluation(executed=False)
    )
    default_benchmark: BenchmarkEvaluation = field(
        default_factory=lambda: BenchmarkEvaluation(executed=False)
    )
    accuracy_results: list[AccuracyEvaluation] = field(default_factory=list)
    benchmark_results: list[BenchmarkEvaluation] = field(default_factory=list)
    accuracy_calls: list[FakeAccuracyCall] = field(default_factory=list)
    benchmark_calls: list[FakeBenchmarkCall] = field(default_factory=list)
    run_id: str = "test-run"

    def script_accuracy(self, *results: AccuracyEvaluation) -> None:
        """Queue accuracy results in call order."""
        self.accuracy_results.extend(results)

    def script_benchmark(self, *results: BenchmarkEvaluation) -> None:
        """Queue benchmark results in call order."""
        self.benchmark_results.extend(results)

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
    ) -> None:
        """Create a host whose private role map derives from ``plugin.agents``."""
        self._run_id = run_id
        self._facts = RunFacts(domain_id="generic") if facts is None else facts
        self._workspaces = FakeWorkspaces(FakeWorkspace(path=project_root))
        self._agents = FakeAgentSessions(
            plugin.agents,
            responder=responder,
            bindings=agent_bindings,
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
