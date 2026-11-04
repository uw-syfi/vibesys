"""One turn executor over several providers: a turn goes where its session spec's provider runs.

An :class:`~vs_agent.client.AgentClient` is bound to one provider, because its launcher, readiness
probe and conversation checkpoints are. A run whose roles use different providers builds one client
per provider and gives the durable session journal this router in their place, so the journal never
learns that there are several.
"""

from __future__ import annotations

from dataclasses import fields
from typing import TYPE_CHECKING

from vs_agent.contracts import AgentCapabilities, SteerOutcome

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_agent.contracts import (
        AgentObserver,
        AgentSessionSpec,
        AgentTurnRequest,
        AgentTurnResult,
    )
    from vs_agent.session_key import AgentSessionKey
    from vs_agent.sessions import AgentTurnExecutor


class ProviderRoutedExecutor:
    """Routes each keyed turn to the executor of ``session_spec.provider``.

    A conversation belongs to the executor that ran its last turn. Before any turn has run
    (a resumed run), ``provider_session_id`` asks the executors in order for the checkpoint the
    shared session store holds, which every executor reads alike. ``capabilities`` is what every
    executor guarantees, so a guarantee never holds for only some roles.
    """

    def __init__(self, executors: Mapping[str, AgentTurnExecutor]) -> None:
        """Route to ``executors``, one per provider; at least one is required."""
        if not executors:
            message = "a provider router needs at least one executor"
            raise ValueError(message)
        self._executors = dict(executors)
        self._owners: dict[AgentSessionKey, AgentTurnExecutor] = {}

    @property
    def capabilities(self) -> AgentCapabilities:
        """The features every routed executor provides."""
        every = [executor.capabilities for executor in self._executors.values()]
        return AgentCapabilities(
            **{
                field.name: all(getattr(capabilities, field.name) for capabilities in every)
                for field in fields(AgentCapabilities)
            }
        )

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """The conversation the next turn on ``session_key`` continues; none when it has none."""
        owner = self._owners.get(session_key)
        if owner is not None:
            return owner.provider_session_id(session_key)
        for executor in self._executors.values():
            found = executor.provider_session_id(session_key)
            if found is not None:
                return found
        return None

    def cancel_session(self, key: AgentSessionKey) -> None:
        """Ask the conversation's in-flight turn to stop; a key no turn ran on is a no-op."""
        owner = self._owners.get(key)
        if owner is not None:
            owner.cancel_session(key)

    def steer_session(self, key: AgentSessionKey, text: str, *, steer_id: str) -> SteerOutcome:
        """Offer ``text`` to the conversation's in-flight turn; a key no turn ran on has none."""
        owner = self._owners.get(key)
        if owner is None:
            return SteerOutcome.NO_RUNNING_TURN
        return owner.steer_session(key, text, steer_id=steer_id)

    def release_session(self, key: AgentSessionKey) -> None:
        """Release the conversation's live resources, keeping its checkpoint."""
        owner = self._owners.get(key)
        if owner is not None:
            owner.release_session(key)

    def run(
        self,
        *,
        session_spec: AgentSessionSpec,
        turn: AgentTurnRequest,
        session_key: AgentSessionKey | None = None,
        observer: AgentObserver | None = None,
    ) -> AgentTurnResult:
        """Run the turn on the executor of ``session_spec.provider``.

        Raises ``ValueError`` naming the provider when no executor serves it.
        """
        executor = self._executors.get(session_spec.provider)
        if executor is None:
            message = (
                f"no agent client serves provider {session_spec.provider!r}; "
                f"served: {', '.join(sorted(self._executors))}"
            )
            raise ValueError(message)
        if session_key is not None:
            self._owners[session_key] = executor
        return executor.run(
            session_spec=session_spec, turn=turn, session_key=session_key, observer=observer
        )
