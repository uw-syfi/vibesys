"""Report the start and end of each provider turn a durable session actually dispatches.

``ClientAgentSessions`` replays a turn its journal already settled and never re-dispatches an
unknown one, so only the call into the keyed client proves that a provider turn began. This
wrapper stands exactly there: every ``run`` is one ``AgentExecutionStarted`` and exactly one
``AgentExecutionFinished`` for the same execution id, however the turn ends, and a replayed
or inspected turn reports nothing. It changes no behavior of the client it wraps.
"""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from vs_agent.api import AgentExecutionCancelledError, AgentOutputSchemaError, AgentTurnExecutor
from vs_core.api import WaitableReply
from vs_decode.api import Unreadable, decode_json
from vs_runtime._agent_lifecycle import (
    AgentExecutionFinished,
    AgentExecutionStarted,
    AgentExecutionStatus,
    AgentReply,
)

if TYPE_CHECKING:
    from vs_agent.api import (
        AgentCapabilities,
        AgentObserver,
        AgentSessionKey,
        AgentSessionSpec,
        AgentTurnRequest,
        AgentTurnResult,
        SteerOutcome,
    )
    from vs_runtime._agent_lifecycle import AgentExecutionLifecycleSink
    from vs_runtime._run_control import RunControlChannel


_MAX_REPLY_BYTES = 1024 * 1024
"""A longer reply is not read as a typed one; its text is still reported."""


@cache
def _json_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def structured_reply(schema: type[BaseModel] | None, text: str) -> AgentReply | None:
    """``text`` read as a reply of the turn's ``schema``, or None when it is not one.

    A role that may wait for evaluations answers in an envelope; the reply is what the envelope
    holds, a result or a wait, and ``kind`` names that model. Text that is no valid reply is the
    turn's failure to report elsewhere, never a guess here.
    """
    if schema is None:
        return None
    try:
        parsed = decode_json(text, schema, source="agent reply", max_bytes=_MAX_REPLY_BYTES)
    except TypeError:
        # A reply model that allows unknown keys cannot be decoded strictly. The report is a
        # reading aid, so the turn goes on without it; the property test over every role's reply
        # model fails for such a model.
        return None
    if isinstance(parsed, Unreadable):
        return None
    reply = parsed.reply if isinstance(parsed, WaitableReply) else parsed
    if not isinstance(reply, BaseModel):
        return None
    model = type(reply)
    return AgentReply(
        kind=model.__name__,
        json_schema=_json_schema(model),
        value=reply.model_dump(mode="json"),
    )


class LifecycleReportingExecutor:
    """An ``AgentTurnExecutor`` that reports each dispatched turn to a lifecycle sink."""

    def __init__(
        self,
        client: AgentTurnExecutor,
        lifecycle: AgentExecutionLifecycleSink,
        *,
        control: RunControlChannel | None = None,
    ) -> None:
        """Wrap *client*; *lifecycle* is called on the thread that runs the turn."""
        self._client = client
        self._lifecycle = lifecycle
        self._control = control

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

    def steer_session(self, key: AgentSessionKey, text: str, *, steer_id: str) -> SteerOutcome:
        """Offer a message to the keyed conversation's in-flight turn."""
        return self._client.steer_session(key, text, steer_id=steer_id)

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
        started = AgentExecutionStarted(
            agent_id=agent_id,
            label=label,
            execution_id=execution_id,
            system_prompt=turn.instructions,
            user_prompt=turn.message,
            provider=session_spec.provider,
            model=session_spec.model,
            reasoning_effort=session_spec.reasoning_effort,
        )

        def publish_start() -> None:
            self._lifecycle(started)

        if self._control is None:
            publish_start()
        else:
            try:
                self._control.start_execution(publish_start)
            except AgentExecutionCancelledError as error:
                message = f"{error}: turn {execution_id} ({agent_id})"
                raise AgentExecutionCancelledError(message) from error

        def finished(
            status: AgentExecutionStatus,
            *,
            result: str | None,
            error: str | None,
            reply: AgentReply | None = None,
        ) -> None:
            self._lifecycle(
                AgentExecutionFinished(
                    agent_id=agent_id,
                    label=label,
                    execution_id=execution_id,
                    status=status,
                    result=result,
                    error=error,
                    reply=reply,
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
        finished(
            AgentExecutionStatus.COMPLETED,
            result=outcome.text,
            error=None,
            reply=structured_reply(turn.output_schema, outcome.text),
        )
        return outcome


def report_turns(
    client: AgentTurnExecutor,
    lifecycle: AgentExecutionLifecycleSink,
    *,
    control: RunControlChannel | None = None,
) -> AgentTurnExecutor:
    """The client with every dispatched turn reported to *lifecycle*."""
    return LifecycleReportingExecutor(client, lifecycle, control=control)


__all__ = ["LifecycleReportingExecutor", "report_turns"]
