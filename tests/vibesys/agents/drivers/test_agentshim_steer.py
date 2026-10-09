"""An operator message reaches a running turn on a transport that can take it.

Codex's long-lived app-server is scripted with ``agentshim.testing.CodexScript``
(its real ``turn/steer`` exchange), so the whole path runs: ``AgentClient.steer``
-> ``AgentShimSession.steer`` -> ``agentshim.Session.steer`` -> the server. The
default one-shot transport must never be asked.
"""

from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Protocol, cast

import agentshim
import pytest
from agentshim.testing import (
    AwaitSteer,
    CodexScript,
    FakeExecutor,
    RunCommand,
    Say,
    scripted_turn,
)
from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentEvent,
    AgentEventKind,
    NullAgentEventSink,
    SteerableSession,
    SteerOutcome,
)
from vs_agent.client import AgentDiagnosticLog
from vs_agent.contracts import (
    AgentExecutionPolicy,
    AgentSession,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox.api import SANDBOX_DISABLE_ENV

if TYPE_CHECKING:
    from collections.abc import Callable


TEXT = "instead, say STEERED"
PROVIDERS = ("claude", "codex", "gemini", "opencode")


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway operator HOME: the driver prepares provider state under it."""
    return tmp_path_factory.mktemp("operator-home")


def _driver(
    provider: str,
    fake: FakeExecutor,
    home: Path,
    transport: agentshim.TransportKind,
) -> AgentShimDriver:
    return AgentShimDriver(
        provider=provider,
        executor_factory=lambda: fake,
        launcher_env=lambda: {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            SANDBOX_DISABLE_ENV: "off",
        },
        transport=transport,
    )


class _SteerableAgentSession(AgentSession, SteerableSession, Protocol):
    """A session that is also the optional steering capability."""


def _steerable(session: AgentSession) -> _SteerableAgentSession:
    """The AgentShim session as the optional steering capability it provides."""
    assert isinstance(session, SteerableSession)
    return cast("_SteerableAgentSession", session)


def _diagnostics(events: list[AgentEvent]) -> list[str]:
    return [
        e.text or ""
        for e in events
        if e.kind is AgentEventKind.THINKING and e.payload.get("channel") == "diagnostic"
    ]


def _spec(root: Path, provider: str = "codex") -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider=provider,
        workspace=root,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )


class _SteerWhenRunning:
    """Offer a steer on the turn's own thread at each event, until one is taken.

    A turn reports setup events before the provider has a turn the message can
    join; those offers answer ``NO_RUNNING_TURN``. Retrying per event is how a
    caller reaches the first moment the turn is live, with no clock involved.
    """

    def __init__(self, offer: Callable[[], SteerOutcome]) -> None:
        self._offer = offer
        self.outcomes: list[SteerOutcome] = []
        self.events: list[AgentEvent] = []

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        if SteerOutcome.DELIVERED not in self.outcomes and SteerOutcome.UNSUPPORTED not in (
            self.outcomes
        ):
            self.outcomes.append(self._offer())

    def diagnostics(self) -> list[str]:
        return [
            e.text or ""
            for e in self.events
            if e.kind is AgentEventKind.THINKING and e.payload.get("channel") == "diagnostic"
        ]


def _delivered(outcomes: list[SteerOutcome]) -> bool:
    """Whether the offers ended in delivery after only not-yet-running answers."""
    return outcomes[-1:] == [SteerOutcome.DELIVERED] and all(
        outcome is SteerOutcome.NO_RUNNING_TURN for outcome in outcomes[:-1]
    )


def test_a_steer_reaches_the_running_turn_through_the_client(tmp_path: Path, home: Path) -> None:
    script = CodexScript().turn(RunCommand("sleep 20"), AwaitSteer(then=(Say("STEERED"),)))
    driver = _driver(
        "codex", FakeExecutor([], peers=script.peer), home, agentshim.TransportKind.STREAM
    )
    rejected: list[str] = []
    steerer = _SteerWhenRunning(
        lambda: client.steer(TEXT, on_rejected=lambda: rejected.append(TEXT))
    )

    class _Sink(NullAgentEventSink):
        """Offers the steer at each callback of the running turn, on the turn's thread."""

        def agent_output(self, *_args: object, **_kwargs: object) -> None:
            steerer.on_event(AgentEvent(kind=AgentEventKind.THINKING))

        def tool_call(self, *_args: object, **_kwargs: object) -> None:
            steerer.on_event(AgentEvent(kind=AgentEventKind.THINKING))

    client = AgentClient(
        driver,
        provider="codex",
        event_sink=_Sink(),
        driver_log=AgentDiagnosticLog(io.StringIO()),
    )

    answer = client.invoke_text(
        kind="implementer",
        workspace=tmp_path,
        system_prompt="sys",
        user_prompt="go",
        round_label="r1",
    )

    assert _delivered(steerer.outcomes)
    assert answer == "STEERED"
    assert rejected == []
    assert len(script.requests("turn/steer")) == 1


def test_a_steer_is_delivered_once_and_reported_on_the_diagnostic_channel(
    tmp_path: Path, home: Path
) -> None:
    script = CodexScript().turn(RunCommand("sleep 20"), AwaitSteer(then=(Say("STEERED"),)))
    driver = _driver(
        "codex", FakeExecutor([], peers=script.peer), home, agentshim.TransportKind.STREAM
    )
    session = _steerable(driver.create_session(_spec(tmp_path)))
    rejected: list[str] = []
    observer = _SteerWhenRunning(
        lambda: session.steer(TEXT, on_rejected=lambda: rejected.append(TEXT))
    )

    result = session.run_turn(AgentTurnRequest(message="go"), observer)

    assert _delivered(observer.outcomes)
    assert result.text == "STEERED"
    assert rejected == []
    assert [d for d in _diagnostics(observer.events) if d.startswith("[steer]")] == [
        "[steer] the provider accepted an operator message mid-turn",
        "[steer] the model took the operator message into the running turn",
    ]


def test_a_refusal_after_acceptance_hands_the_text_back_exactly_once(
    tmp_path: Path, home: Path
) -> None:
    script = CodexScript(steer_refusal="turn cannot be steered").turn(
        Say("a"), AwaitSteer(then=(Say("b"),))
    )
    driver = _driver(
        "codex", FakeExecutor([], peers=script.peer), home, agentshim.TransportKind.STREAM
    )
    session = _steerable(driver.create_session(_spec(tmp_path)))
    rejected: list[str] = []

    def steer_then_stop() -> SteerOutcome:
        outcome = session.steer(TEXT, on_rejected=lambda: rejected.append(TEXT))
        if outcome is SteerOutcome.DELIVERED:
            session.cancel()
        return outcome

    observer = _SteerWhenRunning(steer_then_stop)

    with pytest.raises(agentshim.TurnCancelledError):
        session.run_turn(AgentTurnRequest(message="go"), observer)

    assert _delivered(observer.outcomes)
    assert rejected == [TEXT]
    assert any("refused the operator message" in d for d in _diagnostics(observer.events))


@given(provider=st.sampled_from(PROVIDERS))
def test_a_one_shot_provider_is_never_asked_to_steer(provider: str, home: Path) -> None:
    fake = FakeExecutor(scripted_turn(provider, text="done"))
    driver = _driver(provider, fake, home, agentshim.TransportKind.ONE_SHOT)
    with TemporaryDirectory() as root:
        session = _steerable(driver.create_session(_spec(Path(root), provider)))
        rejected: list[str] = []
        observer = _SteerWhenRunning(
            lambda: session.steer(TEXT, on_rejected=lambda: rejected.append(TEXT))
        )

        result = session.run_turn(AgentTurnRequest(message="go"), observer)

    assert observer.outcomes == [SteerOutcome.UNSUPPORTED]
    assert result.text == "done"
    assert rejected == []
    assert len(fake.requests) == 1  # the turn's own launch: nothing was sent on its behalf


def test_a_steer_with_no_turn_running_reports_it_before_and_after_a_turn(
    tmp_path: Path, home: Path
) -> None:
    script = CodexScript().turn(Say("ok"))
    driver = _driver(
        "codex", FakeExecutor([], peers=script.peer), home, agentshim.TransportKind.STREAM
    )
    session = _steerable(driver.create_session(_spec(tmp_path)))

    def offer() -> SteerOutcome:
        return session.steer(TEXT, on_rejected=lambda: None)

    before = offer()
    session.run_turn(AgentTurnRequest(message="go"))
    after = offer()

    assert (before, after) == (SteerOutcome.NO_RUNNING_TURN, SteerOutcome.NO_RUNNING_TURN)
    assert script.requests("turn/steer") == []


def test_a_client_with_no_turn_in_flight_reports_none_running(home: Path) -> None:
    fake = FakeExecutor(scripted_turn("claude"))
    client = AgentClient(
        _driver("claude", fake, home, agentshim.TransportKind.ONE_SHOT),
        provider="claude",
    )

    assert client.steer(TEXT, on_rejected=lambda: None) is SteerOutcome.NO_RUNNING_TURN
