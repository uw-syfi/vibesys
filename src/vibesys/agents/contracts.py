"""Implementation-neutral contracts for stateful agent execution."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import timedelta
    from pathlib import Path
    from typing import TextIO

    from langchain_core.tools import BaseTool

    from vibesys.agents.progress import AgentProgress
    from vibesys.agents.session_key import AgentSessionKey
    from vs_sandbox import HostResource, ProjectPathPolicy

T = TypeVar("T", bound=BaseModel)


class SessionDisposition(StrEnum):
    """Whether a session remains safe to use after a turn."""

    REUSABLE = "reusable"
    RESET_REQUIRED = "reset_required"


class AgentEventKind(StrEnum):
    """Semantic categories streamed by every driver."""

    TEXT = "text"
    THINKING = "thinking"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    USAGE = "usage"


@dataclass(frozen=True, slots=True)
class AgentExecutionPolicy:
    """Existing sandbox semantics that a driver must enforce for a session.

    Drivers must reject policies they cannot fully enforce. The reusable
    ``vs_sandbox`` library remains the authority for project-path and host-
    resource semantics; this value only attaches them to agent setup.
    """

    project_paths: ProjectPathPolicy | None = None
    host_resources: tuple[HostResource, ...] = ()
    require_enforcement: bool = True
    containerized: bool = False


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """Provider-independent description of a session-scoped stdio MCP server."""

    name: str
    command: str
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AgentCapabilities:
    """Features a driver can provide without weakening requested semantics."""

    mcp_servers: bool = False
    in_process_tools: bool = False
    nested_read_only_paths: bool = False
    hidden_paths: bool = False
    host_path_grants: bool = False
    container_execution: bool = False
    timeouts: bool = False
    session_reuse: bool = True
    # Whether a session can adopt a provider conversation ID produced by an
    # earlier process, so a resumed run continues the same conversation instead
    # of replaying it. ``session_reuse`` only promises reuse within one process.
    provider_session_resume: bool = False


@dataclass(frozen=True, slots=True)
class AgentUsage:
    """Provider-independent token and cost accounting for one turn."""

    input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One normalized event emitted while an agent turn is running."""

    kind: AgentEventKind
    text: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    usage: AgentUsage | None = None


class AgentObserver(Protocol):
    """Consumer of normalized streaming events from an agent driver."""

    def on_event(self, event: AgentEvent) -> None:
        """Observe one event in driver emission order."""
        ...


@dataclass(frozen=True, slots=True)
class AgentSessionSpec:
    """Immutable configuration installed once for a conversation."""

    role: str
    provider: str
    workspace: Path
    policy: AgentExecutionPolicy
    model: str | None = None
    mcp_servers: tuple[MCPServerSpec, ...] = ()
    skills: tuple[Path, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()
    reasoning_effort: str | None = None


def session_spec_fingerprint(spec: AgentSessionSpec) -> str:
    """Return a stable digest of the whole session spec.

    ``AgentClient`` drops a cached session as soon as its spec stops matching
    the requested one. A resumed process has no earlier spec object to compare
    against, so it compares this digest instead: same inputs, same rule, so any
    configuration change that would evict a live session also refuses a
    checkpointed provider conversation. The digest is content-derived rather
    than a Python ``hash``, which is not stable across processes.
    """
    return hashlib.sha256(repr(spec).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AgentTurnRequest:
    """Context added to an existing conversation for one agent turn."""

    message: str
    instructions: str = ""
    output_schema: type[BaseModel] | None = None
    timeout: timedelta | None = None
    invocation_id: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class AgentTurnResult:
    """Provider-independent result of one raw agent turn."""

    text: str
    usage: AgentUsage = field(default_factory=AgentUsage)
    provider_session_id: str | None = None
    disposition: SessionDisposition = SessionDisposition.REUSABLE


class AgentSession(Protocol):
    """A configured agent conversation owned by an :class:`AgentDriver`."""

    def run_turn(
        self,
        request: AgentTurnRequest,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Add one turn to the conversation and return its raw result."""
        ...

    def resume_provider_session(self, session_id: str) -> bool:
        """Continue ``session_id`` on this session's next turn.

        Return whether the ID was actually adopted. A driver returns ``False``
        when its provider cannot resume, or when the session already holds a
        live conversation whose history is newer than the caller's checkpoint.
        Callers must not assume adoption: the return value, not the call, is
        what says the next turn resumes.

        Implementations that report ``False`` must leave the session usable and
        unchanged. Drivers whose capabilities set ``provider_session_resume``
        to ``False`` always return ``False``.
        """
        ...

    def close(self) -> None:
        """Release session resources. Implementations must be idempotent."""
        ...


class AgentDriver(Protocol):
    """Adapter that creates configured sessions using one execution system."""

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this driver can enforce."""
        ...

    def create_session(self, spec: AgentSessionSpec) -> AgentSession:
        """Create a session or reject any unsupported part of ``spec``."""
        ...

    def close(self) -> None:
        """Release driver resources. Implementations must be idempotent."""
        ...


class AgentClientProtocol(Protocol):
    """The agent-service surface the run context and every loop depend on.

    Each backend supplies one implementation: the CLI
    :class:`~vibesys.agents.client.AgentClient`, the deterministic stub, the
    deepagents runner, and the plain loop's tracker wrapper. Attribution
    (``backend_name``, ``driver_name``, ``provider``, ``model_for_kind``) is
    part of this contract because the loop stamps it onto every round record,
    so a consumer never has to probe an implementation for it.
    """

    @property
    def backend_name(self) -> str:
        """Return the configured agent backend (``"cli"``, ``"stub"``, ...)."""
        ...

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the features this client's execution system can enforce."""
        ...

    @property
    def driver_name(self) -> str | None:
        """Return the stable configured driver name, or ``None`` when unnamed."""
        ...

    @property
    def provider(self) -> str | None:
        """Return the provider that runs this client's turns."""
        ...

    def model_for_kind(self, kind: str) -> str | None:
        """Return the effective model for ``kind``, honoring role overrides."""
        ...

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation the next turn on ``session_key`` continues.

        ``None`` means the next turn starts from no history at all.
        """
        ...

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Name the provider conversation ``session_key``'s last turn ran in."""
        ...

    def invoke(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> T:
        """Run one turn and parse its structured response."""
        ...

    def invoke_text(  # noqa: PLR0913
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        mcp_servers: list[MCPServerSpec] | None = None,
        tools: list[BaseTool] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Run one conversational turn without a structured-output requirement."""
        ...

    def set_log_file(self, stream: TextIO | None) -> None:
        """Direct subsequent application logs to ``stream``."""
        ...

    def close(self) -> None:
        """Release client resources. Implementations must be idempotent."""
        ...
