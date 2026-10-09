"""Operation scenarios shared by every catalog test, so a new operation gets them all.

Each scenario is one declared operation with a real owner, one request, and a
durable count of how many times its effect really happened. The mirrored request
models stand in for the strategy-declared operations, which a library test cannot
import. Add a scenario here and the shared crash and replay tests cover it.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from pydantic import BaseModel

from vs_core.api import (
    ArtifactRef,
    ExecuteRegisteredOperation,
    LifecycleClass,
    OperationDescriptor,
    OperationId,
    OperationRegistration,
    OperationRegistry,
    OperationRequest,
    OperationWire,
    RequestId,
    RevisionId,
    RevisionRef,
    RunId,
    SchemaRef,
    Scope,
    Value,
)
from vs_project.api import StateNamespace
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import ArtifactStore
from vs_runtime.api.core import (
    Applied,
    Cancelled,
    ExecutionContext,
    Indeterminate,
    Inspection,
    NotApplied,
    OperationCatalog,
    OperationEntry,
    OperationRole,
    RenderArtifactsOwner,
    VerifyRevisionOwner,
    build_operation_catalog,
)
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces

SCOPE = Scope(owner=RunId(root="run"), generation=0)


class SimulatedCrashError(
    BaseException
):  # lint-waiver: LW-0D3-7 [N818]; a BaseException subclass models a process kill that no owner or executor may catch.
    """The process died here."""


def _registration(
    kind: str,
    request: type[OperationRequest],
    outcome: type[BaseModel],
    lifecycle: LifecycleClass,
    *,
    cancel: bool = False,
) -> OperationRegistration:
    stem = kind.replace(".", "-")
    return OperationRegistration(
        descriptor=OperationDescriptor(
            kind=kind,
            request_schema=SchemaRef(name=stem, version=1),
            outcome_schema=SchemaRef(name=f"{stem}-outcome", version=1),
            lifecycle=lifecycle,
            inspect=True,
            cancel=cancel,
        ),
        request_model=request,
        outcome_model=outcome,
    )


# A plain idempotent write whose effect is one line in a durable log.


class EchoOutcome(Value):
    status: Literal["succeeded"] = "succeeded"
    text: str


class EchoRequest(OperationRequest):
    kind: Literal["test.echo"] = "test.echo"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = EchoOutcome
    text: str


class EffectLog:
    """Durable count of real effects, surviving the executor that caused them."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, line: str) -> None:
        with self._path.open("a") as stream:
            stream.write(line + "\n")

    def lines(self) -> list[str]:
        return self._path.read_text().splitlines() if self._path.exists() else []


class EchoOwner:
    """Cancellable owner. Execute performs one durable effect and inspect reads it back."""

    def __init__(self, log: EffectLog) -> None:
        self.log = log
        self.cancelled: list[str] = []
        self.hide_effects = False
        self.failures_left = 0
        self.output: BaseModel | Mapping[str, object] | None = None

    async def execute(
        self, request: OperationRequest, context: ExecutionContext
    ) -> BaseModel | Mapping[str, object]:
        del context
        assert isinstance(request, EchoRequest)
        if self.failures_left:
            self.failures_left -= 1
            message = "owner lost its connection before the effect"
            raise ConnectionError(message)
        self.log.append(request.text)
        return self.output if self.output is not None else EchoOutcome(text=request.text)

    async def inspect(self, request: OperationRequest, context: ExecutionContext) -> Inspection:
        del context
        assert isinstance(request, EchoRequest)
        if self.hide_effects:
            return Indeterminate("owner cannot read its effect")
        if request.text in self.log.lines():
            return Applied(EchoOutcome(text=request.text))
        return NotApplied()

    async def cancel(self, request: OperationRequest, context: ExecutionContext) -> Cancelled:
        del context
        assert isinstance(request, EchoRequest)
        self.cancelled.append(request.text)
        return Cancelled("stopped")


# Mirrors of the strategy-declared operations, served by the real owners.


class PromptContext(Value):
    template: Literal["greeting"] = "greeting"
    who: str


class RenderedArtifacts(Value):
    status: Literal["succeeded", "failed"]
    prompts: tuple[ArtifactRef, ...] = ()
    tool_policy: ArtifactRef | None = None


class RenderRoleArtifacts(OperationRequest):
    kind: Literal["test.render"] = "test.render"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = RenderedArtifacts
    subject: str
    ordinal: int
    context: PromptContext


class ParentVerification(Value):
    status: Literal["succeeded"] = "succeeded"
    verified: bool
    detail: str = ""


class VerifyParentRevision(OperationRequest):
    kind: Literal["test.verify"] = "test.verify"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = ParentVerification
    parent: RevisionRef


class Readings(Value):
    status: Literal["succeeded"] = "succeeded"


class InterpretEvidence(OperationRequest):
    kind: Literal["test.interpret"] = "test.interpret"
    lifecycle: Literal[LifecycleClass.QUERY] = LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = Readings
    evidence: tuple[str, ...]


class Retained(Value):
    status: Literal["succeeded"] = "succeeded"
    retained: bool


class RetainVerifiedRevision(OperationRequest):
    kind: Literal["test.retain"] = "test.retain"
    lifecycle: Literal[LifecycleClass.IDEMPOTENT_WRITE] = LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = Retained
    revision: RevisionRef


def revision(commit: str) -> RevisionRef:
    return RevisionRef(revision_id=RevisionId(root=commit), digest=f"git-commit:{commit}")


def commit_of(ref: RevisionRef) -> str | None:
    commit = ref.revision_id.root
    return commit if ref.digest == f"git-commit:{commit}" else None


@dataclass(frozen=True)
class OperationScenario:
    """One catalog entry, one request for it, and a durable count of its real effects."""

    name: str
    entry: OperationEntry
    request: OperationRequest
    effects: Callable[[], int]
    refused: bool = False

    @property
    def allows_repeat(self) -> bool:
        """Queries have no effect, so repeating one is not an effect."""
        return self.entry.registration.descriptor.lifecycle is LifecycleClass.QUERY


SCENARIO_NAMES = ("echo", "render", "verify", "interpret", "retain")


def scenarios(root: Path, namespace: StateNamespace) -> tuple[OperationScenario, ...]:
    """Every catalog entry, wired to real owners below ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    log = EffectLog(root / "effects.log")
    templates = root / "templates"
    templates.mkdir(exist_ok=True)
    (templates / "greeting.j2").write_text("hello {{ who }}\n")
    store = ArtifactStore(namespace)
    workspace = FakeWorkspace(known_revisions={"abc"})
    workspaces = FakeWorkspaces(workspace)
    artifacts = root_artifacts(namespace)
    echo_entry = OperationEntry.owned(
        _registration(
            "test.echo", EchoRequest, EchoOutcome, LifecycleClass.IDEMPOTENT_WRITE, cancel=True
        ),
        EchoOwner(log),
    )
    built = build_operation_catalog(
        {
            OperationRole.RENDER_ARTIFACTS: _registration(
                "test.render",
                RenderRoleArtifacts,
                RenderedArtifacts,
                LifecycleClass.IDEMPOTENT_WRITE,
            ),
            OperationRole.VERIFY_REVISION: _registration(
                "test.verify", VerifyParentRevision, ParentVerification, LifecycleClass.QUERY
            ),
            OperationRole.INTERPRET_EVIDENCE: _registration(
                "test.interpret", InterpretEvidence, Readings, LifecycleClass.QUERY
            ),
            OperationRole.RETAIN_REVISION: _registration(
                "test.retain", RetainVerifiedRevision, Retained, LifecycleClass.IDEMPOTENT_WRITE
            ),
        },
        {
            OperationRole.RENDER_ARTIFACTS: RenderArtifactsOwner(
                TemplateRenderer(templates), store
            ),
            OperationRole.VERIFY_REVISION: VerifyRevisionOwner(workspaces, workspaces, commit_of),
        },
        extra=(echo_entry,),
    )
    entries = {entry.registration.descriptor.kind: entry for entry in built.entries}
    return (
        OperationScenario(
            "echo",
            entries["test.echo"],
            EchoRequest(text="one"),
            lambda: len(log.lines()),
        ),
        OperationScenario(
            "render",
            entries["test.render"],
            RenderRoleArtifacts(subject="planner", ordinal=0, context=PromptContext(who="world")),
            lambda: len(list(artifacts.iterdir())) if artifacts.exists() else 0,
        ),
        OperationScenario(
            "verify",
            entries["test.verify"],
            VerifyParentRevision(parent=revision("abc")),
            lambda: 0,
        ),
        OperationScenario(
            "interpret",
            entries["test.interpret"],
            InterpretEvidence(evidence=("e",)),
            lambda: 0,
            refused=True,
        ),
        OperationScenario(
            "retain",
            entries["test.retain"],
            RetainVerifiedRevision(revision=revision("abc")),
            lambda: 0,
            refused=True,
        ),
    )


def root_artifacts(namespace: StateNamespace) -> Path:
    return namespace.external_directory("artifacts")


def catalog_of(items: tuple[OperationScenario, ...]) -> OperationCatalog:
    registry = OperationRegistry(tuple(item.entry.registration for item in items))
    return OperationCatalog(registry, tuple(item.entry for item in items))


def execute_request(
    catalog: OperationCatalog, request: OperationRequest, name: str
) -> ExecuteRegisteredOperation:
    wire: OperationWire = catalog.registry.encode(request)
    return ExecuteRegisteredOperation(
        request_id=RequestId(root=name),
        scope=SCOPE,
        deadline_at=100.0,
        operation_id=OperationId(root=f"op:{name}"),
        operation=wire,
        retry_limit=0,
    )
