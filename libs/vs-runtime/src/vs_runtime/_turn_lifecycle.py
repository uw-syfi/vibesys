"""Report the start and end of each provider turn a durable session actually dispatches.

``ClientAgentSessions`` replays a turn its journal already settled and never re-dispatches an
unknown one, so only the call into the keyed client proves that a provider turn began. This
wrapper stands exactly there: every ``run`` is one ``AgentExecutionStarted`` and exactly one
``AgentExecutionFinished`` for the same execution id, however the turn ends, and a replayed
or inspected turn reports nothing. It changes no behavior of the client it wraps.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_agent.api import AgentOutputSchemaError, AgentTurnExecutor
from vs_runtime._agent_lifecycle import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
)

if TYPE_CHECKING:
    from vs_agent.api import (
        AgentCapabilities,
        AgentObserver,
        AgentSessionKey,
        AgentSessionSpec,
        AgentTurnRequest,
        AgentTurnResult,
    )
    from vs_runtime._agent_lifecycle import AgentExecutionLifecycleSink


class LifecycleReportingExecutor:
    """An ``AgentTurnExecutor`` that reports each dispatched turn to a lifecycle sink."""

    def __init__(self, client: AgentTurnExecutor, lifecycle: AgentExecutionLifecycleSink) -> None:
        """Wrap *client*; *lifecycle* is called on the thread that runs the turn."""
        self._client = client
        self._lifecycle = lifecycle

    @property
    def capabilities(self) -> AgentCapabilities:
        """What the wrapped client guarantees."""
        return self._client.capabilities

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """The conversation the next keyed turn continues."""
        return self._client.provider_session_id(session_key)

    def cancel_session(self, key: AgentSessionKey) -> None:
        """Ask the keyed conversation's in-flight turn to stop."""
        self._client.cancel_session(key)

    def release_session(self, key: AgentSessionKey) -> None:
        """Release the keyed conversation's live resources."""
        self._client.release_session(key)

    def run(
        self,
        *,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        session_key: AgentSessionKey | None = None,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Run the keyed turn between its start and its one finish."""
        agent_id = session_spec.role
        label = turn.label or agent_id
        execution_id = turn.invocation_id or ""
        self._lifecycle(
            AgentExecutionStarted(
                agent_id=agent_id,
                label=label,
                execution_id=execution_id,
                system_prompt=turn.instructions,
                user_prompt=turn.message,
                provider=session_spec.provider,
                model=session_spec.model,
            )
        )

        def finished(
            status: AgentExecutionStatus, *, result: str | None, error: str | None
        ) -> None:
            self._lifecycle(
                AgentExecutionFinished(
                    agent_id=agent_id,
                    label=label,
                    execution_id=execution_id,
                    status=status,
                    result=result,
                    error=error,
                )
            )

        try:
            outcome = self._client.run(
                session_spec=session_spec, turn=turn, session_key=session_key, observer=observer
            )
        except AgentOutputSchemaError as error:
            finished(AgentExecutionStatus.FAILED, result=None, error=error.detail)
            raise
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            finished(AgentExecutionStatus.FAILED, result=None, error=failure)
            raise
        except BaseException:
            finished(AgentExecutionStatus.CANCELLED, result=None, error="cancelled")
            raise
        finished(AgentExecutionStatus.COMPLETED, result=outcome.text, error=None)
        return outcome


def report_turns(
    client: AgentTurnExecutor, lifecycle: AgentExecutionLifecycleSink
) -> AgentTurnExecutor:
    """The client with every dispatched turn reported to *lifecycle*."""
    return LifecycleReportingExecutor(client, lifecycle)


__all__ = ["LifecycleReportingExecutor", "report_turns"]
