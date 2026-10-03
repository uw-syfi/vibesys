"""Faults at the agent-turn boundary: a wrapper over any agent client.

:class:`FaultyAgentClient` implements ``AgentClientProtocol`` by delegating to
an inner client and, on the turns its plan schedules, replacing the turn's
outcome with the failure a real agent CLI produces. It knows no roles: wrong
and invalid replies are generated from the schema the turn declares.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, ValidationError

from vs_agent.api import AgentOutputSchemaError, AgentTurnTimeoutError, describe_validation_error
from vs_faults.plan import AgentFault, Boundary, FaultPlan
from vs_faults.replies import ReplyGenerator, prompt_vocabulary

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import (
        AgentCapabilities,
        AgentClientProtocol,
        AgentProgress,
        AgentSessionKey,
        ToolServerDescriptor,
    )
    from vs_agent.api.testing import FakeInvocation

T = TypeVar("T", bound=BaseModel)

#: The turn budget a faulted turn reports exceeding, as the CLI driver does.
TURN_BUDGET_S = 3600.0
_NO_JSON = "the reply contained no JSON object"


class AgentCrashError(RuntimeError):
    """The agent CLI process exited mid-turn (an injected fault)."""


def _validate(payload: object, response_cls: type[T]) -> T:
    try:
        return response_cls.model_validate(payload)
    except ValidationError as error:
        raise AgentOutputSchemaError(describe_validation_error(error)) from error


def generated_replies(plan: FaultPlan) -> Callable[[FakeInvocation], BaseModel]:
    """Return a Fake-client responder that answers every turn from its declared schema.

    Each turn draws from a generator seeded by the plan seed, the role, and
    the role's turn count, so a run's replies are reproducible from the seed
    as far as the run's own turn order is.
    """
    counts: Counter[str] = Counter()
    lock = threading.Lock()

    def answer(invocation: FakeInvocation) -> BaseModel:
        assert invocation.response_cls is not None
        with lock:
            counts[invocation.kind] += 1
            ordinal = counts[invocation.kind]
        generator = ReplyGenerator(
            plan.rng("reply", invocation.kind, ordinal), prompt_vocabulary(invocation.user_prompt)
        )
        reply = generator.valid(invocation.response_cls)
        if reply is None:
            raise AgentOutputSchemaError(_NO_JSON)
        return reply

    return answer


class FaultyAgentClient:
    """An ``AgentClientProtocol`` that injects its plan's agent-turn faults.

    With no agent-turn rules it is a pass-through. Turns are counted per role
    (``kind``); a rule fires on its ``at``-th turn of its role, or of any role
    when it names none. ``injected`` records each fault that fired.
    """

    def __init__(self, inner: AgentClientProtocol, plan: FaultPlan) -> None:
        """Wrap ``inner``; ``plan`` decides which turns fail and how."""
        self._inner = inner
        self._plan = plan
        self._counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self.injected: list[tuple[str, int, AgentFault]] = []

    @property
    def backend_name(self) -> str:
        """Return the inner client's backend."""
        return self._inner.backend_name

    @property
    def capabilities(self) -> AgentCapabilities:
        """Return the inner client's capabilities."""
        return self._inner.capabilities

    @property
    def driver_name(self) -> str | None:
        """Return the inner client's driver."""
        return self._inner.driver_name

    @property
    def provider(self) -> str | None:
        """Return the inner client's provider."""
        return self._inner.provider

    def model_for_kind(self, kind: str) -> str | None:
        """Return the inner client's model for ``kind``."""
        return self._inner.model_for_kind(kind)

    def provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Return the inner client's next provider session."""
        return self._inner.provider_session_id(session_key)

    def last_turn_provider_session_id(self, session_key: AgentSessionKey) -> str | None:
        """Return the inner client's last provider session."""
        return self._inner.last_turn_provider_session_id(session_key)

    def set_log_file(self, stream: TextIO | None) -> None:
        """Forward the log stream."""
        self._inner.set_log_file(stream)

    def cancel(self) -> None:
        """Forward cancellation."""
        self._inner.cancel()

    def close(self) -> None:
        """Forward close."""
        self._inner.close()

    def _fault(self, kind: str) -> tuple[AgentFault | None, int]:
        with self._lock:
            self._counts[kind] += 1
            ordinal = self._counts[kind]
            rule = self._plan.match(Boundary.AGENT_TURN, kind, ordinal)
            fault = rule.fault if rule is not None else None
            assert fault is None or isinstance(fault, AgentFault)
            if fault is not None:
                self.injected.append((kind, ordinal, fault))
        return fault, ordinal

    def invoke(  # noqa: PLR0913  # LW-150004 [PLR0913]; the wrapper implements AgentClientProtocol.invoke, whose keyword contract (LW-010126) it cannot narrow.
        self,
        *,
        kind: str,
        workspace: Path,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        round_label: str,
        env: dict[str, str] | None = None,
        invocation_id: str | None = None,
        progress: AgentProgress | None = None,
        tool_servers: list[ToolServerDescriptor] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> T:
        """Run one structured turn, or fail it as the plan schedules."""
        fault, ordinal = self._fault(kind)

        def inner() -> T:
            return self._inner.invoke(
                kind=kind,
                workspace=workspace,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                response_cls=response_cls,
                round_label=round_label,
                env=env,
                invocation_id=invocation_id,
                progress=progress,
                tool_servers=tool_servers,
                reuse_session=reuse_session,
                session_key=session_key,
            )

        if fault is None:
            return inner()
        generator = ReplyGenerator(
            self._plan.rng("fault", kind, ordinal), prompt_vocabulary(user_prompt)
        )
        if fault is AgentFault.MALFORMED:
            raise AgentOutputSchemaError(_NO_JSON)
        if fault is AgentFault.SCHEMA_INVALID:
            return _validate(generator.invalid(response_cls), response_cls)
        if fault is AgentFault.WRONG_VALUES:
            reply = generator.valid(response_cls)
            if reply is None:
                raise AgentOutputSchemaError(_NO_JSON)
            assert isinstance(reply, response_cls)
            return reply
        # The remaining faults strike after the agent did its work.
        reply = inner()
        if fault is AgentFault.EXTRA_KEYS:
            return _validate({**reply.model_dump(mode="json"), "notes": "extra"}, response_cls)
        if fault is AgentFault.TIMEOUT:
            raise AgentTurnTimeoutError(TURN_BUDGET_S)
        message = f"agent CLI exited with code 1 during turn {ordinal} of {kind}"
        raise AgentCrashError(message)

    def invoke_text(  # noqa: PLR0913  # LW-150005 [PLR0913]; the wrapper implements AgentClientProtocol.invoke_text, whose keyword contract (LW-010127) it cannot narrow.
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
        tool_servers: list[ToolServerDescriptor] | None = None,
        reuse_session: bool | None = None,
        session_key: AgentSessionKey | None = None,
    ) -> str:
        """Run one text turn; transport faults apply, output faults do not."""
        fault, ordinal = self._fault(kind)
        text = self._inner.invoke_text(
            kind=kind,
            workspace=workspace,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            round_label=round_label,
            env=env,
            invocation_id=invocation_id,
            progress=progress,
            tool_servers=tool_servers,
            reuse_session=reuse_session,
            session_key=session_key,
        )
        if fault is AgentFault.TIMEOUT:
            raise AgentTurnTimeoutError(TURN_BUDGET_S)
        if fault is AgentFault.CRASH:
            message = f"agent CLI exited with code 1 during turn {ordinal} of {kind}"
            raise AgentCrashError(message)
        return text
