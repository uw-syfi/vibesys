"""A durable session world for the SESSIONS executor: real disk, the Fake agent client.

The provider conversation lives in one ``AgentClient`` over ``FakeDriver`` and the
invocation journal in one ``FakeAgentInvocationStore``; both outlive a simulated
host restart. Each ``executor`` call builds new ``ClientAgentSessions`` and a new
executor over the same disk, so a restart forgets exactly what a real one forgets
(in-flight ownership) and keeps what a real one keeps (journal, provider conversation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

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
    TurnSpec,
)
from vs_prompts.api import RenderedPrompt, TemplateRenderer
from vs_runtime.api.core import ReceiptStore, RuntimeSessionRequests

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.core import SessionResolver

SCOPE = Scope(owner=RunId(root="run"), generation=0)
ROLE = RoleId(root="worker")
SCHEMA = SchemaRef(name="reply", version=1)
SESSION = SessionId(root="session-1")


class Reply(BaseModel):
    """The structured reply the Fake driver returns."""

    value: int


def session_spec(*, policy: str = "fresh", session: SessionId = SESSION) -> SessionSpec:
    """A run-owned session of the declared role."""
    return SessionSpec(
        session_id=session,
        role_id=ROLE,
        policy=policy,  # type: ignore[arg-type]
        lifetime="owner",
        access=Access.READ_ONLY,
    )


def ensure_request(request_id: str = "req-ensure", **spec: object) -> EnsureSession:
    """An EnsureSession for the default session."""
    return EnsureSession(
        request_id=RequestId(root=request_id),
        scope=SCOPE,
        deadline_at=100.0,
        spec=session_spec(**spec),  # type: ignore[arg-type]
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
    return ensure_request(request_id, policy="reuse").model_copy(
        update={"required_resource": resource}
    )


@dataclass
class FakeSessionResolver:
    """Resolves the one declared role, schema and prompts; a test can make any of them unknown."""

    workspace: Path
    renderer: TemplateRenderer
    roles: frozenset[RoleId] = frozenset({ROLE})
    schemas: dict[SchemaRef, type[BaseModel]] = field(default_factory=lambda: {SCHEMA: Reply})
    messages_resolve: bool = True

    def knows_role(self, role: RoleId) -> bool:
        """Whether the role is declared."""
        return role in self.roles

    def agent_spec(self, turn: TurnSpec) -> AgentSessionSpec | None:
        """The Fake provider's session configuration."""
        return AgentSessionSpec(
            role=turn.session.role_id.root,
            provider="fake",
            workspace=self.workspace,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )

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


@dataclass
class SessionHost:
    """The durable pieces that survive a host restart."""

    resolver: FakeSessionResolver
    client: AgentClient
    journal: FakeAgentInvocationStore
    turns: list[AgentTurnRequest]

    def executor(self, store: ReceiptStore) -> RuntimeSessionRequests:
        """A freshly started host over the same journal and provider conversation."""
        resolver: SessionResolver = self.resolver
        return RuntimeSessionRequests(FakeAgentSessions(self.client, self.journal), resolver, store)

    def sessions(self) -> FakeAgentSessions:
        """A freshly started journal reader, for assertions on durable facts."""
        return FakeAgentSessions(self.client, self.journal)


def open_host(workspace: Path, *, answer: dict[str, object] | None = None) -> SessionHost:
    """A host whose Fake provider answers every turn with *answer* and records each turn."""
    turns: list[AgentTurnRequest] = []
    client = AgentClient(FakeDriver(answer=answer or {"value": 7}, on_turn=turns.append))
    return SessionHost(
        FakeSessionResolver(workspace, TemplateRenderer(workspace)),
        client,
        FakeAgentInvocationStore(),
        turns,
    )


def receipts(namespace: object) -> ReceiptStore:
    """A receipt store over *namespace*."""
    return ReceiptStore(namespace)  # type: ignore[arg-type]
