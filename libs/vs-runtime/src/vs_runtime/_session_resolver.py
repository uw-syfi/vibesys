"""The production ``SessionResolver``: core declarations in, agent-turn inputs out.

Everything a session executor needs to turn a core ``TurnSpec`` into a provider turn:
the run's role registry, its reply-schema registry, the live workspace a scope names,
the role's access grant and turn timeout, and the turn message. The message is
rendered from the packaged ``session_turn.j2`` template over the prompt and input
artifacts; Python only reads the artifacts (each verified against the digest the
reference carries) and passes their text as data.

The provider session configuration (sandbox, mounts, tool servers) is run composition
knowledge, so it arrives as the injected ``session_spec`` factory, the same way the
legacy path takes ``resolve_configuration``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import AgentTurnRequest, ClientAgentSessions, MCPServerSpec
from vs_core.api import Access, Scope
from vs_prompts.api import TemplateRenderer
from vs_runtime._artifact_store import ArtifactStoreError
from vs_runtime._session_lifecycle_requests import SessionLifecycleRequests
from vs_runtime._session_requests import RuntimeSessionRequests
from vs_runtime._workspace_access import AccessGrant
from vs_runtime._workspace_lookup import find_scope_workspace
from vs_runtime.contracts import RuntimeContractError, WorkspaceAccess

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic import BaseModel

    from vs_agent.api import (
        AgentInvocationStore,
        AgentSessionSpec,
        AgentTurnExecutor,
        ToolServerDescriptor,
    )
    from vs_core.api import (
        ArtifactRef,
        RoleId,
        SchemaRef,
        SessionInput,
        TurnSpec,
        WorkspaceRef,
    )
    from vs_prompts.api import RenderedPrompt
    from vs_runtime._artifact_store import ArtifactStore
    from vs_runtime._receipt_store import ReceiptStore
    from vs_runtime._session_requests import SessionResolver, TurnYields
    from vs_runtime._workspace_access import AccessGuardedWorkspace
    from vs_runtime._workspace_receipts import WorkspaceReceipts
    from vs_runtime._workspaces import RuntimeWorkspaces
    from vs_runtime.api.infrastructure import AgentConfigurationResolver
    from vs_runtime.contracts import AgentRole

type SessionSpecFactory = Callable[[AgentRole, Path], AgentSessionSpec]
"""Provider session configuration for one role in one workspace directory."""

_TEMPLATE = "session_turn.j2"
_RANK = {WorkspaceAccess.READ_ONLY: 0, WorkspaceAccess.LIMITED: 1, WorkspaceAccess.READ_WRITE: 2}


class ToolServerSource(Protocol):
    """Extra tool servers one role's turns in one scope are offered."""

    def servers(self, role: AgentRole, scope: Scope) -> tuple[ToolServerDescriptor, ...]:
        """The servers for this role and scope; empty when the role is offered none."""
        ...


@dataclass(frozen=True)
class ResolverInputs:
    """What the run supplies once; the resolver derives every per-turn answer from it."""

    roles: tuple[AgentRole, ...]
    schemas: Mapping[SchemaRef, type[BaseModel]]
    workspaces: RuntimeWorkspaces
    workspace_receipts: WorkspaceReceipts
    artifacts: ArtifactStore
    configuration: AgentConfigurationResolver
    session_spec: SessionSpecFactory
    artifact_directories: tuple[str, ...] = ()
    """Workspace-relative directories a ``write-artifacts`` turn may write."""
    tool_servers: ToolServerSource | None = None
    """Agent tool servers bound per role and scope; None offers the role no extra tool."""
    renderer: TemplateRenderer | None = None
    """Renders ``session_turn.j2``; defaults to the packaged template root."""


class ProductionSessionResolver:
    """Resolve roles, schemas, workspaces, grants, timeouts and messages for the run."""

    def __init__(self, inputs: ResolverInputs) -> None:
        """Index the declared roles; two roles with one id are a declaration error."""
        roles = {role.id: role for role in inputs.roles}
        if len(roles) != len(inputs.roles):
            message = "agent role ids must be unique"
            raise RuntimeContractError(message)
        self._inputs = inputs
        self._roles = roles
        self._renderer = inputs.renderer or TemplateRenderer(Path(__file__).parent / "prompts")

    def _role(self, role: RoleId) -> AgentRole | None:
        return self._roles.get(role.root)

    def knows_role(self, role: RoleId) -> bool:
        """Whether *role* is declared for this run."""
        return self._role(role) is not None

    def output_schema(self, ref: SchemaRef) -> type[BaseModel] | None:
        """The registered reply type for *ref*."""
        return self._inputs.schemas.get(ref)

    async def workspace_for(self, ref: WorkspaceRef | Scope) -> AccessGuardedWorkspace | None:
        """The live run or attempt workspace that *ref* names."""
        return await find_scope_workspace(
            self._inputs.workspaces, self._inputs.workspace_receipts, ref
        )

    def guard_snapshots(self, fenced_by: Callable[[Path], tuple[str, ...]]) -> None:
        """Install the durable fence on the run's workspaces, root and candidates."""
        self._inputs.workspaces.guard_access(fenced_by)

    def access_grant(self, turn: TurnSpec) -> AccessGrant | None:
        """What the turn may write: the requested access, never more than the role declares."""
        role = self._role(turn.session.role_id)
        if role is None:
            return None
        requested, paths = {
            Access.READ_ONLY: (WorkspaceAccess.READ_ONLY, ()),
            Access.WRITE_ARTIFACTS: (WorkspaceAccess.LIMITED, self._inputs.artifact_directories),
            Access.WRITE_CANDIDATE: (WorkspaceAccess.READ_WRITE, ()),
        }[turn.session.access]
        allowed = min(requested, role.workspace_access, key=_RANK.__getitem__)
        limited = allowed is WorkspaceAccess.LIMITED
        return AccessGrant(
            role_id=role.id,
            access=allowed,
            paths=paths if limited else (),
            directories=paths if limited else (),
        )

    def agent_spec(
        self, turn: TurnSpec, workspace: AccessGuardedWorkspace
    ) -> AgentSessionSpec | None:
        """The provider session for the turn's role in its workspace."""
        role = self._role(turn.session.role_id)
        if role is None:
            return None
        spec = self._inputs.session_spec(role, workspace.path)
        source = self._inputs.tool_servers
        if source is None:
            return spec
        scope = turn.workspace if isinstance(turn.workspace, Scope) else turn.workspace.scope
        extra = tuple(
            MCPServerSpec(item.name, item.command, item.args, item.env, item.runtime_env)
            for item in source.servers(role, scope)
        )
        return replace(spec, mcp_servers=(*spec.mcp_servers, *extra))

    def turn_timeout(self, role: RoleId) -> timedelta | None:
        """The role's resolved in-turn timeout; one constant for every turn of the role."""
        declared = self._role(role)
        if declared is None:
            return None
        seconds = self._inputs.configuration(declared).spec.cli_timeout
        return None if seconds is None else timedelta(seconds=seconds)

    def template(self, turn: TurnSpec) -> AgentTurnRequest | None:
        """The role's fixed instructions and one constant label."""
        role = self._role(turn.session.role_id)
        if role is None:
            return None
        return AgentTurnRequest(
            message="", instructions=role.system_prompt, label=f"{role.id}-session-turn"
        )

    def message(self, turn: TurnSpec, inputs: tuple[SessionInput, ...]) -> RenderedPrompt | None:
        """The turn message, or None when any artifact is missing, corrupt or unreadable."""
        ordered = sorted(inputs, key=lambda item: (item.sequence, item.input_id.root))
        try:
            prompts = [self._text(ref) for ref in turn.prompts]
            texts = [
                {"mode": item.mode.value, "text": self._text(item.artifact)} for item in ordered
            ]
            for dependency in turn.artifact_dependencies:
                self._text(dependency)
        except (ArtifactStoreError, UnicodeDecodeError, _MissingArtifactError):
            return None
        return self._renderer.render_template(_TEMPLATE, prompts=prompts, inputs=texts)

    def _text(self, ref: ArtifactRef) -> str:
        content = self._inputs.artifacts.read_digest(ref.digest)
        if content is None:
            raise _MissingArtifactError
        return content.decode()


class _MissingArtifactError(Exception):
    """An artifact reference names no stored object."""


@dataclass(frozen=True)
class SessionExecutors:
    """The session executors of one run, built over one shared access settlement."""

    turns: RuntimeSessionRequests
    """EnsureSession, DispatchTurn and InspectTurn."""
    lifecycle: SessionLifecycleRequests
    """CancelTurn, CloseSession and ResumeSessionTurn."""


def open_session_requests(
    inputs: ResolverInputs,
    *,
    client: AgentTurnExecutor,
    invocation_slot: AgentInvocationStore,
    store: ReceiptStore,
    yields: TurnYields | None = None,
) -> SessionExecutors:
    """The session executors over the run's agent client, journal slot and receipt store.

    The one entry point run wiring calls to give core real agent turns: build the
    ``ResolverInputs`` from the run's declarations, pass the machine-local invocation
    journal slot and the shared receipt store, and register each executor for its
    request kinds. Both executors share the one access settlement, and building them
    installs its durable snapshot fence on the run's workspaces, so no executor can
    exist without it.
    """
    return session_executors(
        ClientAgentSessions(client, invocation_slot),
        ProductionSessionResolver(inputs),
        store,
        yields,
    )


def session_executors(
    sessions: ClientAgentSessions,
    resolver: SessionResolver,
    store: ReceiptStore,
    yields: TurnYields | None = None,
) -> SessionExecutors:
    """Both session executors over one access settlement, for any resolver.

    ``open_session_requests`` is this over the production resolver; run wiring and tests
    that supply their own resolver call this, so the shared settlement and the snapshot
    fence it installs cannot be forgotten.
    """
    turns = RuntimeSessionRequests(sessions, resolver, store, yields)
    return SessionExecutors(
        turns, SessionLifecycleRequests(sessions, turns, store, turns.settlement)
    )


__all__ = [
    "ProductionSessionResolver",
    "ResolverInputs",
    "SessionExecutors",
    "SessionSpecFactory",
    "ToolServerSource",
    "open_session_requests",
    "session_executors",
]
