"""Pause the run when a provider has no capacity left, and send the turn again on resume."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_agent.api import AgentEventSink, AgentQuotaError, AgentTurnRequest
    from vs_runtime._run_control import RunControlChannel


class PausingCapacityGate:
    """Turn a provider capacity limit into a run pause that the operator resumes.

    The run's existing cooperative pause carries the state: the turn that hit
    the limit stays in flight on its worker thread, the run reports PAUSING and
    then PAUSED, and the typed ``quota_paused`` event says why. Resuming
    releases the thread and the same turn is sent again, so a run that waited
    and resumed on its provider has the same experiment state as one that never
    stopped. A stop request ends the wait by raising ``RunStopped``.
    """

    def __init__(self, control: RunControlChannel, events: AgentEventSink) -> None:
        """Bind the gate to the run's control channel and its semantic event sink."""
        self._control = control
        self._events = events

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str
    ) -> None:
        """Pause the run, publish why, and return once the operator resumes it."""
        context = {
            "agent_kind": role,
            "round_label": turn.label,
            "invocation_id": turn.invocation_id,
        }
        self._events.quota_paused(error, **context)
        self._control.request_pause()
        self._control.wait_while_paused()
        self._events.quota_resumed(error.provider, **context)
