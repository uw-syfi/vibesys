"""Runtime ownership of product-composed auxiliary agent conversations."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import AgentSessionKey, SessionScope

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor


class _Closeable(Protocol):
    """One resource whose close operation is idempotent."""

    def close(self) -> None:
        """Release the resource."""
        ...


@dataclass(frozen=True, slots=True)
class ManagedConversationSpec:
    """Fixed execution policy for one auxiliary conversation."""

    role: str
    member_id: str
    workspace: Path
    system_prompt: str
    continuation_prompt: str | None = None
    tool_servers: tuple[ToolServerDescriptor, ...] = ()
    environment: tuple[tuple[str, str], ...] = ()


class ManagedConversation(Protocol):
    """A serialized, context-preserving, explicitly closeable conversation."""

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Send one follow-on message in this conversation."""
        ...

    def close(self) -> None:
        """Release this conversation and every transferred resource."""
        ...


class _RuntimeManagedConversation:
    """Thread-safe owner of one client's provider conversation and resources."""

    def __init__(
        self,
        client: AgentClientProtocol,
        spec: ManagedConversationSpec,
        resources: tuple[_Closeable, ...],
    ) -> None:
        for name, value in (("role", spec.role), ("member_id", spec.member_id)):
            if not value.strip():
                error = f"managed conversation {name} must not be empty"
                raise ValueError(error)
        self._client = client
        self._spec = spec
        self._resources = resources
        self._session_key = AgentSessionKey(
            SessionScope.MEMBER,
            f"{spec.member_id}:{uuid.uuid4().hex}",
        )
        self._lock = threading.Lock()
        self._closed = False

    def turn(self, message: str, *, invocation_id: str | None = None) -> str:
        """Serialize a turn and reuse only this object's provider context."""
        if not message.strip():
            error = "managed conversation turn message must not be empty"
            raise ValueError(error)
        with self._lock:
            if self._closed:
                error = "managed conversation is closed"
                raise RuntimeError(error)
            previous = self._client.provider_session_id(self._session_key)
            prompt = (
                self._spec.continuation_prompt
                if previous is not None and self._spec.continuation_prompt is not None
                else self._spec.system_prompt
            )
            answer = self._invoke(message, prompt, invocation_id=invocation_id)
            if (
                previous is not None
                and prompt == self._spec.continuation_prompt
                and self._client.last_turn_provider_session_id(self._session_key) != previous
            ):
                return self._invoke(
                    message,
                    self._spec.system_prompt,
                    invocation_id=invocation_id,
                )
            return answer

    def _invoke(
        self,
        message: str,
        system_prompt: str,
        *,
        invocation_id: str | None,
    ) -> str:
        return self._client.invoke_text(
            kind=self._spec.role,
            workspace=self._spec.workspace,
            system_prompt=system_prompt,
            env=dict(self._spec.environment),
            user_prompt=message,
            round_label=f"auxiliary {self._spec.role}",
            invocation_id=invocation_id,
            progress=None,
            reuse_session=True,
            session_key=self._session_key,
            tool_servers=list(self._spec.tool_servers),
        )

    def close(self) -> None:
        """Wait for an active turn, then close transferred resources in reverse."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            first_error: BaseException | None = None
            for resource in reversed(self._resources):
                try:
                    resource.close()
                # lint-waiver: LW-948028 [BLE001]; cleanup must attempt every transferred resource and preserve the first failure.
                # > Catching Exception misses cancellation and process-exit failures; a helper
                # > would still require the same broad catch while obscuring reverse ownership.
                except BaseException as exc:  # noqa: BLE001
                    first_error = first_error or exc
            self._resources = ()
            if first_error is not None:
                raise first_error


def create_managed_conversation(
    client: AgentClientProtocol,
    spec: ManagedConversationSpec,
    *,
    resources: tuple[_Closeable, ...],
) -> ManagedConversation:
    """Transfer *resources* after validating and creating one conversation.

    If construction raises, ownership remains with the caller.  After a
    successful return, the conversation closes every resource in reverse order.
    """
    return _RuntimeManagedConversation(client, spec, resources)


__all__ = [
    "ManagedConversation",
    "ManagedConversationSpec",
    "create_managed_conversation",
]
