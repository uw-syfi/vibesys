"""Fast deterministic agent client for end-to-end interface smoke tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from vs_agent.contracts import AgentCapabilities, AgentOutputSchemaError
from vs_agent.sink import NULL_AGENT_EVENT_SINK, AgentEventSink

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.progress import AgentProgress
    from vs_agent.session_key import AgentSessionKey
    from vs_agent.tools import ToolServerDescriptor
T = TypeVar("T", bound=BaseModel)

_NO_STRUCTURED_OUTPUT = "the stub backend writes no structured output"


class StubAgentClient:
    """Exercise the agent-client contract without invoking an external agent."""

    backend_name = "stub"

    def __init__(
        self,
        *,
        event_sink: AgentEventSink = NULL_AGENT_EVENT_SINK,
    ) -> None:
        """Create a stateless deterministic client."""
        self._sink = event_sink

    @property
    def capabilities(self) -> AgentCapabilities:
        """Emulate the conversation capabilities used by interface smoke runs."""
        return AgentCapabilities(
            tool_servers=True,
            session_reuse=True,
            provider_session_resume=True,
        )

    @property
    def driver_name(self) -> str | None:
        """No driver runs a stub turn; the stub itself is the attribution."""
        return "stub"

    @property
    def provider(self) -> str | None:
        """No external provider runs a stub turn."""
        return "stub"

    def model_for_kind(self, kind: str) -> str | None:
        """A stub turn runs no model, for any role."""
        del kind
        return None

    def close(self) -> None:
        """The deterministic stub owns no external resources."""

    def cancel(self) -> None:
        """The deterministic stub has no in-flight external work to stop."""

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Never name a conversation: the stub runs no provider at all."""
        del session_key
        return None

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Never name a conversation: the stub runs no provider at all."""
        del session_key
        return None

    def set_log_file(self, stream: object) -> None:
        """Accept log retargeting; the deterministic stub emits no file logs."""
        del stream

    def invoke(  # noqa: PLR0913  # lint-waiver: LW-010191 [PLR0913]; Preserve StubAgentClient.invoke's named-argument contract because callers pass these independent settings directly.
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        round_label: str,
        progress: AgentProgress | None = None,
        **kwargs: object,
    ) -> T:
        """Report one requested structured turn as producing no structured output.

        The stub runs no agent, so it has no response to return; it raises
        :class:`AgentOutputSchemaError` like a real agent whose reply never
        matched the schema.
        """
        del workspace, system_prompt, user_prompt, response_cls, progress, kwargs
        self._sink.agent_output(
            f"[stub-agent] {round_label}: starting {kind}\n",
            channel="diagnostic",
            agent_kind=kind,
        )
        time.sleep(0.05)
        self._sink.agent_output(
            f"[stub-agent] {round_label}: completed {kind}\n",
            channel="diagnostic",
            agent_kind=kind,
        )
        raise AgentOutputSchemaError(_NO_STRUCTURED_OUTPUT)

    def invoke_text(  # noqa: PLR0913  # lint-waiver: LW-010192 [PLR0913]; Preserve StubAgentClient.invoke_text's named-argument contract because callers pass these independent settings directly.
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        env: dict[str, str] | None = None,
        user_prompt: str,
        round_label: str,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        tool_servers: list[ToolServerDescriptor] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Return a deterministic answer for auxiliary-agent smoke tests."""
        del (
            workspace,
            system_prompt,
            env,
            progress,
            tool_servers,
            reuse_session,
            session_key,
        )
        self._sink.agent_output(
            f"[stub-agent] investigating: {user_prompt}\n",
            channel="analysis",
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
        )
        return "Stub agent inspected the available experiment trajectory."
