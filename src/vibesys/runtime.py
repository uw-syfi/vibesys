"""Public capabilities available to a custom orchestration while it executes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.api import (
        AgentCapabilities,
        AgentSessionKey,
        AgentSpec,
        SessionScope,
        ToolServerDescriptor,
    )
    from vs_sandbox.api import HostResource

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    """One named agent with its own backend/model choice and host grants."""

    id: str
    spec: AgentSpec
    resources: tuple[HostResource, ...] = ()


@dataclass(slots=True)
class WorkspaceScope:
    """An isolated candidate tree and its current retained revision."""

    id: str
    path: Path
    revision: str


class AgentHandle(Protocol):
    """A live agent conversation owned by one runtime."""

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return features the selected driver can enforce."""
        ...

    @property
    def backend_name(self) -> str:
        """Return the selected agent backend."""
        ...

    @property
    def driver_name(self) -> str | None:
        """Return the selected CLI driver, when configured."""
        ...

    @property
    def provider(self) -> str | None:
        """Return the selected provider, when configured."""
        ...

    @property
    def model(self) -> str | None:
        """Return the model used for this role."""
        ...

    async def turn(
        self,
        message: str,
        *,
        system_prompt: str = "",
        label: str = "",
    ) -> str:
        """Send one turn and return its text; labels are policy-defined."""
        ...

    async def turn_structured(  # noqa: PLR0913  # lint-waiver: LW-020037 [PLR0913]; AgentHandle.turn_structured is the public typed-turn contract, and its keyword options are independent.
        self,
        message: str,
        *,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        system_prompt: str = "",
        label: str = "",
        session_key: AgentSessionKey | None = None,
        reuse_session: bool | None = None,
        tool_servers: list[ToolServerDescriptor] | None = None,
    ) -> T:
        """Run a typed turn, retaining the caller's fallback and session policy."""
        ...

    async def close(self) -> None:
        """Release this agent and its sandbox; safe to call more than once."""
        ...


@dataclass(frozen=True, slots=True)
class ReadOnly:
    """A role may only read the workspace; ``ctx.agents.turn`` reverts the rest.

    ``allow`` names paths (files or directories, matched by prefix) the role
    may still write, e.g. the designer's own roadmap index or a profiler's
    bounded evidence directory.
    """

    allow: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Writes:
    """A role may freely modify the workspace (an implementer, most agents)."""


@dataclass(frozen=True, slots=True)
class Fresh:
    """Start a new agent session for every turn; never reused."""


@dataclass(frozen=True, slots=True)
class Reuse:
    """Let the agent client pick its own default session.

    Passes ``reuse_session=None`` and no ``session_key`` to
    ``AgentHandle.turn_structured``, which reuses one conversation per agent
    role (``AgentSessionKey(SessionScope.ROLE, kind)``) rather than a
    caller-scoped key. This is the client's own default when a caller passes
    neither argument; ``Fresh``/``Keyed`` cannot express it because they
    always pass an explicit ``reuse_session`` bool.
    """


@dataclass(frozen=True, slots=True)
class Keyed:
    """Reuse one session across turns that share a caller-derived key.

    ``scope`` names the session's namespace (e.g. one session per
    hypothesis); the caller supplies the per-call key via ``session_key`` on
    ``ctx.agents.turn``.
    """

    scope: SessionScope


@dataclass(frozen=True, slots=True)
class Role:
    """One agent role: its prompt, reply contract, and turn policy.

    Declared once per role family in ``vibesys.roles``; strategies invoke it
    through ``ctx.agents.turn(role, ...)`` and never hand-roll rendering,
    isolation, timeout fallback, or correction retries themselves.

    ``check`` validates the reply in isolation (no external state): it is a
    pure function of the parsed reply, run up to ``max_corrections`` times.
    A correction that depends on run state (e.g. "this hypothesis ID was
    already used this run") is not expressible here and stays a strategy-
    level retry around plain ``ctx.agents.turn`` calls.
    """

    id: str
    template: str
    reply: type[BaseModel]
    fallback: Callable[[], BaseModel]
    context: type[BaseModel]
    """The pydantic model this role's template renders from. ``ctx.agents.turn``
    accepts any ``Mapping | BaseModel`` as ``context``, but every role
    declares one so a contract test can assert its fields match the
    template's free variables (see ``tests/vibesys/roles/test_prompt_contracts.py``)."""
    access: ReadOnly | Writes = field(default_factory=Writes)
    session: Fresh | Keyed | Reuse = field(default_factory=Fresh)
    paid: bool = False
    check: Callable[[BaseModel], str | None] | None = None
    max_corrections: int = 0
    filter_skills: bool = False
    message: str = "Return only the JSON object."
    timeout_fallback: Callable[[float], BaseModel] | None = None
    """Optional distinct reply for ``subprocess.TimeoutExpired``, given the
    configured timeout in seconds. Defaults to ``fallback()`` (called with no
    arguments) when unset, so existing roles are unaffected."""


class RoleIsolationError(RuntimeError):
    """A read-only role left unauthorized workspace changes after restoration."""

    def __init__(self, remaining: list[str], *, role: str) -> None:
        """Name the role and paths that could not be isolated."""
        super().__init__(
            f"Cannot isolate {role}: workspace is still modified after restore: "
            + ", ".join(remaining[:8])
        )


class CorrectionExhaustedError(RuntimeError):
    """A role's correction loop ended without a reply that passed ``check``."""

    def __init__(self, role: str) -> None:
        """Name the role whose correction budget was exhausted."""
        super().__init__(f"{role}: correction loop exited without a valid reply")


class VibeSysRuntime(Protocol):
    """Run-scoped host for agents; orchestration owns scheduling and messages.

    Spawned agents and their sandboxes close when execution ends. A policy may
    close a handle earlier. `resources` are passed to sandbox provisioning and
    agent-driver enforcement, so a driver unable to enforce them rejects spawn.
    """

    @property
    def workspace(self) -> Path:
        """Return the prepared run's writable project workspace."""
        ...

    async def spawn(
        self, definition: AgentDefinition, *, scope: WorkspaceScope | None = None
    ) -> AgentHandle:
        """Open an agent in the parent or a live isolated workspace."""
        ...
