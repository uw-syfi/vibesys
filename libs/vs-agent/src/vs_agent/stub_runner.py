"""Fast deterministic agent client for end-to-end interface smoke tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from vs_agent.contracts import AgentCapabilities, MCPServerSpec
from vs_agent.fake_response import AgentResponseContext, AgentResponseScenario
from vs_agent.sink import NULL_AGENT_EVENT_SINK, AgentEventSink

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_agent.progress import AgentProgress
    from vs_agent.session_key import AgentSessionKey
T = TypeVar("T", bound=BaseModel)


class StubAgentClient:
    """Return deterministic responses without invoking an external agent."""

    backend_name = "stub"

    def __init__(
        self,
        *,
        event_sink: AgentEventSink = NULL_AGENT_EVENT_SINK,
        response_scenario: AgentResponseScenario | None = None,
    ) -> None:
        """Create a deterministic client with an optional structured scenario."""
        self._sink = event_sink
        self._response_scenario = response_scenario
        self._turn_number = 0

    @property
    def capabilities(self) -> AgentCapabilities:
        """The deterministic stub does not expose external tools."""
        return AgentCapabilities(session_reuse=False)

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
        fallback_factory: Callable[[], T],
        round_label: str,
        progress: AgentProgress | None = None,
        **kwargs: object,
    ) -> T:
        """Emit a deterministic stub response for one requested agent turn."""
        del workspace, system_prompt, user_prompt, progress, kwargs
        self._turn_number += 1
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
        if self._response_scenario is None:
            return fallback_factory()
        response = self._response_scenario.respond(
            AgentResponseContext(
                role=kind,
                output_schema=response_cls,
                round_label=round_label,
                turn_number=self._turn_number,
            )
        )
        if response is None:
            return fallback_factory()
        if isinstance(response, BaseModel):
            return response  # ty: ignore[invalid-return-type]
        return response_cls.model_validate(response)

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
        mcp_servers: list[MCPServerSpec] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Return a deterministic answer for auxiliary-agent smoke tests."""
        del (
            workspace,
            system_prompt,
            env,
            progress,
            mcp_servers,
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
