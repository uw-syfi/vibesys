"""A durable session world for the SESSIONS executor: real disk, the Fake agent client.

The provider conversation lives in one ``AgentClient`` over ``FakeDriver`` and the
invocation journal in one ``FakeAgentInvocationStore``; both outlive a simulated
host restart. Each ``executor`` call builds new ``ClientAgentSessions`` and a new
executor over the same disk, so a restart forgets exactly what a real one forgets
(in-flight ownership) and keeps what a real one keeps (journal, provider conversation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel
from tests.support.executor_context import context_for

from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeAgentSessions, FakeDriver
from vs_core.api import (
    Access,
    ArtifactId,
    ArtifactRef,
    DispatchTurn,
    EnsureSession,
    InspectTurn,
    InvocationId,
    InvocationRef,
    RequestId,
    ResourceId,
    RoleId,
    RunId,
    SchemaRef,
    Scope,
    SessionId,
    SessionInput,
    SessionSpec,
    SnapshotAndRetainRun,
    TurnSpec,
    WorkspaceRef,
)
from vs_prompts.api import RenderedPrompt, TemplateRenderer
from vs_runtime.api.core import (
    AccessGrant,
    ExecutionResult,
    ReceiptStore,
    RuntimeSessionRequests,
)
from vs_runtime.api.testing import FakeWorkspace
from vs_runtime.contracts import WorkspaceAccess

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from tests.support.executor_context import RevocableLease

    from vs_core.api import RequestBase
    from vs_runtime.api.core import AccessGuardedWorkspace, SessionResolver

SCOPE = Scope(owner=RunId(root="run"), generation=0)
ROLE = RoleId(root="worker")
SCHEMA = SchemaRef(name="reply", version=1)
SESSION = SessionId(root="session-1")


class Reply(BaseModel):
    """The structured reply the Fake driver returns."""

    value: int


def session_spec(
    *, policy: Literal["fresh", "reuse"] = "fresh", session: SessionId = SESSION
) -> SessionSpec:
    """A run-owned session of the declared role."""
    return SessionSpec(
        session_id=session,
        role_id=ROLE,
        policy=policy,
        lifetime="owner",
        access=Access.READ_ONLY,
    )


def ensure_request(
    request_id: str = "req-ensure", policy: Literal["fresh", "reuse"] = "fresh"
) -> EnsureSession:
    """An EnsureSession for the default session."""
    return EnsureSession(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        spec=session_spec(policy=policy),
    )


def turn_spec(invocation: str = "inv-1", *, session: SessionId = SESSION) -> TurnSpec:
    """A free turn of the default session."""
    return TurnSpec(
        session=session_spec(session=session),
        invocation_id=InvocationId(root=invocation),
        workspace=SCOPE,
        prompts=(ArtifactRef(artifact_id=ArtifactId(root="prompt"), digest="sha256:0"),),
        output_schema=SCHEMA,
        deadline_at=100.0,
        charge_class="free",
    )


def dispatch_request(
    request_id: str = "req-dispatch",
    invocation: str = "inv-1",
    inputs: tuple[SessionInput, ...] = (),
) -> DispatchTurn:
    """A DispatchTurn of one invocation."""
    return DispatchTurn(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        turn=turn_spec(invocation),
        inputs=inputs,
    )


def inspect_request(request_id: str = "req-inspect", invocation: str = "inv-1") -> InspectTurn:
    """An InspectTurn of one invocation."""
    return InspectTurn(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        invocation=InvocationRef(
            session_id=SESSION, invocation_id=InvocationId(root=invocation), generation=0
        ),
    )


def reuse_ensure(request_id: str, resource: ResourceId | None) -> EnsureSession:
    """A reuse-policy EnsureSession, optionally naming the resource it must reattach."""
    return ensure_request(request_id, "reuse").model_copy(update={"required_resource": resource})


def run_snapshot(invocation: str = "inv-1") -> SnapshotAndRetainRun:
    """A run snapshot request for one invocation of the default session."""
    return SnapshotAndRetainRun(
        request_id=RequestId(root="req-snap"),
        scope=SCOPE,
        deadline_at=100.0,
        invocation=InvocationRef(
            session_id=SESSION, invocation_id=InvocationId(root=invocation), generation=0
        ),
        retention="candidate",
    )


@dataclass
class FakeSessionResolver:
    """Resolves the one declared role, schema and prompts; a test can make any of them unknown."""

    workspace: Path
    renderer: TemplateRenderer
    guarded: AccessGuardedWorkspace | None = None
    access: Access = Access.READ_ONLY
    timeout: timedelta | None = timedelta(seconds=30)
    grant_paths: tuple[str, ...] = ()
    grant_directories: tuple[str, ...] = ()
    roles: frozenset[RoleId] = frozenset({ROLE})
    schemas: dict[SchemaRef, type[BaseModel]] = field(default_factory=lambda: {SCHEMA: Reply})
    messages_resolve: bool = True

    def knows_role(self, role: RoleId) -> bool:
        """Whether the role is declared."""
        return role in self.roles

    async def workspace_for(self, ref: WorkspaceRef | Scope) -> AccessGuardedWorkspace | None:
        """The one workspace of this world."""
        del ref
        if self.guarded is None:
            self.guarded = FakeWorkspace(path=self.workspace)
        return self.guarded

    def access_grant(self, turn: TurnSpec) -> AccessGrant | None:
        """Read-only unless a test says otherwise."""
        access = {
            Access.READ_ONLY: WorkspaceAccess.READ_ONLY,
            Access.WRITE_ARTIFACTS: WorkspaceAccess.LIMITED,
            Access.WRITE_CANDIDATE: WorkspaceAccess.READ_WRITE,
        }[self.access]
        return AccessGrant(
            role_id=turn.session.role_id.root,
            access=access,
            paths=self.grant_paths,
            directories=self.grant_directories,
        )

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The Fake provider's session configuration."""
        return AgentSessionSpec(
            role=turn.session.role_id.root,
            provider="fake",
            workspace=workspace.path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )

    def turn_timeout(self, role: RoleId) -> timedelta | None:
        """The one declared in-turn timeout of every role."""
        del role
        return self.timeout

    def template(self, turn: TurnSpec) -> AgentTurnRequest | None:
        """The fixed per-role turn configuration."""
        del turn
        return AgentTurnRequest(message="", instructions="Work.")

    def output_schema(self, ref: SchemaRef) -> type[BaseModel] | None:
        """The registered reply type."""
        return self.schemas.get(ref)

    def message(self, turn: TurnSpec, inputs: tuple[SessionInput, ...]) -> RenderedPrompt | None:
        """A rendered turn message that names the invocation and its input count."""
        if not self.messages_resolve:
            return None
        return self.renderer.render_string(
            "turn {{ invocation }} with {{ count }} inputs",
            invocation=turn.invocation_id.root,
            count=len(inputs),
        )


class CrashOnReplace(ReceiptStore):
    """A receipt store whose host dies at the first record it overwrites.

    After a provider turn, every overwrite (settling its workspace access, sealing the
    request's result) comes after the journal wrote the outcome.
    """

    def replace(self, family: str, part: str, key: str, receipt: BaseModel) -> None:
        del family, part, key, receipt
        raise SystemExit


@dataclass
class ProviderFaults:
    """Scheduled provider failure: while ``down``, a turn is accepted and then dies."""

    down: bool = False


@dataclass
class SessionHost:
    """The durable pieces that survive a host restart."""

    resolver: FakeSessionResolver
    client: AgentClient
    journal: FakeAgentInvocationStore
    turns: list[AgentTurnRequest]
    faults: ProviderFaults

    def executor(self, store: ReceiptStore) -> RuntimeSessionRequests:
        """A freshly started host over the same journal and provider conversation."""
        resolver: SessionResolver = self.resolver
        return RuntimeSessionRequests(FakeAgentSessions(self.client, self.journal), resolver, store)

    def sessions(self) -> FakeAgentSessions:
        """A freshly started journal reader, for assertions on durable facts."""
        return FakeAgentSessions(self.client, self.journal)

    async def run(
        self,
        request: RequestBase,
        store: ReceiptStore,
        *,
        lease: RevocableLease | None = None,
        now_at: float | None = None,
        digest: str | None = None,
    ) -> ExecutionResult:
        """Execute *request* once on a freshly started host over *store*."""
        context = context_for(request, lease=lease)
        if now_at is not None:
            context = context.model_copy(update={"now_at": now_at})
        if digest is not None:
            context = context.model_copy(update={"payload_digest": digest})
        outcome = await self.executor(store).execute(cast("Any", request), context)
        assert isinstance(outcome, ExecutionResult), outcome
        return outcome

    def restart_lost_conversation(self, workspace: Path) -> SessionHost:
        """A host with the same journal but a provider that has forgotten every conversation."""
        lost = open_host(workspace)
        lost.journal = self.journal
        return lost


def open_host(
    workspace: Path,
    *,
    answer: dict[str, object] | None = None,
    effect: Callable[[], None] | None = None,
) -> SessionHost:
    """A host whose Fake provider answers every turn with *answer*, records it, runs *effect*."""
    turns: list[AgentTurnRequest] = []
    faults = ProviderFaults()

    def on_turn(request: AgentTurnRequest) -> None:
        turns.append(request)
        if effect is not None:
            effect()
        if faults.down:
            message = "provider died after accepting the turn"
            raise ConnectionError(message)

    client = AgentClient(FakeDriver(answer=answer or {"value": 7}, on_turn=on_turn))
    return SessionHost(
        FakeSessionResolver(workspace, TemplateRenderer(workspace)),
        client,
        FakeAgentInvocationStore(),
        turns,
        faults,
    )


class SettledRunInvocations:
    """A run-invocation proof that holds every invocation's writer ended (a Fake)."""

    def unproven(self, request: object) -> None:
        """Every invocation is proven terminal."""
        del request


@dataclass
class RunningRunInvocations:
    """A run-invocation proof for a writer that has not ended (a Fake)."""

    reason: str = "the writer is still running"

    def unproven(self, request: object) -> str:
        """No invocation is proven terminal."""
        del request
        return self.reason
