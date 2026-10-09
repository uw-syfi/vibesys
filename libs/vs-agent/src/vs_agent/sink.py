"""Injected event-emission seam for agent-package code.

Agent code (callbacks, the CLI client, the stub client) has no event-stream or
rendering ownership. Callers construct agent services with an
:class:`AgentEventSink`; product wiring adapts these callbacks to its own
run-scoped semantic stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vs_agent.contracts import (
        AgentQuotaError,
        AgentRateLimit,
        Attribution,
        ProviderSwitch,
        QuotaPlan,
        QuotaResumeReason,
    )
    from vs_agent.events import (
        AgentOutputChannel,
        AgentStatusData,
        TodoItemData,
        ToolResultPayload,
    )


@runtime_checkable
class AgentEventSink(Protocol):
    """Agent callbacks a product-owned semantic event adapter implements."""

    def agent_output(  # noqa: PLR0913  # lint-waiver: LW-010182 [PLR0913]; Preserve AgentEventSink.agent_output's named-argument contract because callers pass these independent settings directly.
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish one text chunk from an agent."""
        ...

    def tool_call(  # noqa: PLR0913  # lint-waiver: LW-010183 [PLR0913]; Preserve AgentEventSink.tool_call's named-argument contract because callers pass these independent settings directly.
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish an agent tool call and its arguments."""
        ...

    def tool_result(  # noqa: PLR0913  # lint-waiver: LW-010184 [PLR0913]; Preserve AgentEventSink.tool_result's named-argument contract because callers pass these independent settings directly.
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish a tool result, including error state when applicable."""
        ...

    def todo_update(
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish the agent's current todo list."""
        ...

    def usage_update(  # noqa: PLR0913  # lint-waiver: LW-010185 [PLR0913]; Preserve AgentEventSink.usage_update's named-argument contract because callers pass these independent settings directly.
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish token usage from the current model response."""
        ...

    def rate_limit_update(
        self,
        rate_limit: AgentRateLimit,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish one rate-limit window the provider reported."""
        ...

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        """Publish that a turn stopped on a provider capacity limit and the run paused."""
        ...

    def quota_resumed(self, provider: str, reason: QuotaResumeReason, where: Attribution) -> None:
        """Publish that the paused turn is being sent again on the same provider."""
        ...

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        """Publish that the turn ended with the quota error instead of waiting or resuming."""
        ...

    def provider_switched(self, switch: ProviderSwitch, where: Attribution) -> None:
        """Publish that sessions opened from now on use the fallback provider."""
        ...


class NullAgentEventSink:
    """No-op :class:`AgentEventSink` — the default when no sink is injected.

    Stateless and side-effect free, so one process-wide instance is safe to
    share as a default argument.
    """

    __slots__ = ()

    def agent_output(  # noqa: PLR0913  # lint-waiver: LW-010186 [PLR0913]; Preserve NullAgentEventSink.agent_output's named-argument contract because callers pass these independent settings directly.
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore agent output when no event sink was injected."""
        del content, channel, status, agent_kind, round_label, invocation_id

    def tool_call(  # noqa: PLR0913  # lint-waiver: LW-010187 [PLR0913]; Preserve NullAgentEventSink.tool_call's named-argument contract because callers pass these independent settings directly.
        self,
        tool: str,
        args: dict[str, Any],
        *,
        call_id: str | None = None,
        status: AgentStatusData | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore tool calls when no event sink was injected."""
        del tool, args, call_id, status, agent_kind, round_label, invocation_id

    def tool_result(  # noqa: PLR0913  # lint-waiver: LW-010188 [PLR0913]; Preserve NullAgentEventSink.tool_result's named-argument contract because callers pass these independent settings directly.
        self,
        tool: str,
        content: str,
        *,
        call_id: str | None = None,
        is_error: bool = False,
        payload: ToolResultPayload | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore tool results when no event sink was injected."""
        del tool, content, call_id, is_error, payload, agent_kind, round_label, invocation_id

    def todo_update(
        self,
        todos: list[TodoItemData],
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore todo updates when no event sink was injected."""
        del todos, agent_kind, round_label, invocation_id

    def usage_update(  # noqa: PLR0913  # lint-waiver: LW-010189 [PLR0913]; Preserve NullAgentEventSink.usage_update's named-argument contract because callers pass these independent settings directly.
        self,
        input_tokens: int,
        *,
        context_window: int | None = None,
        model: str | None = None,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore usage updates when no event sink was injected."""
        del input_tokens, context_window, model, agent_kind, round_label, invocation_id

    def rate_limit_update(
        self,
        rate_limit: AgentRateLimit,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Ignore rate-limit reports when no event sink was injected."""
        del rate_limit, agent_kind, round_label, invocation_id

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        """Ignore quota pauses when no event sink was injected."""
        del error, plan, where

    def quota_resumed(self, provider: str, reason: QuotaResumeReason, where: Attribution) -> None:
        """Ignore quota resumes when no event sink was injected."""
        del provider, reason, where

    def quota_abandoned(self, error: AgentQuotaError, reason: str, where: Attribution) -> None:
        """Ignore quota abandonment when no event sink was injected."""
        del error, reason, where

    def provider_switched(self, switch: ProviderSwitch, where: Attribution) -> None:
        """Ignore provider switches when no event sink was injected."""
        del switch, where


NULL_AGENT_EVENT_SINK = NullAgentEventSink()
"""Shared no-op sink instance — the default wherever ``AgentEventSink`` is injected.

A single frozen instance (rather than calling the constructor at each call
site) satisfies the "no function calls in argument defaults" lint rule and
makes the shared default explicit.
"""
