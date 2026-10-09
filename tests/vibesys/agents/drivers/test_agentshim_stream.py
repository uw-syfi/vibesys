"""A container agent keeps one long-lived provider process per conversation.

The scripts below play the far end of the process: Claude's stream-json CLI and
Codex's app-server, in agentshim's own fakes, so the real transports parse the
real frames. Which providers take this path is read from agentshim's registry
(``stream_provider_names``); a provider added there fails
``test_every_stream_provider_has_a_script`` until it has a script here.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

import agentshim
import pytest
from agentshim.testing import (
    AwaitSteer,
    ClaudePeerTurn,
    ClaudeStreamPeers,
    CodexScript,
    FakeExecutor,
    Hang,
    ReportRateLimits,
    Say,
    scripted_turn,
)
from tests.support.fake_docker_sandbox import FakeDockerSandbox

from vs_agent.contracts import (
    AgentEvent,
    AgentEventKind,
    AgentExecutionPolicy,
    AgentSessionSpec,
    AgentTurnRequest,
    SteerableSession,
    SteerOutcome,
)
from vs_agent.drivers import agentshim as subject

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from agentshim.execution.process import SpawnRequest
    from agentshim.testing import FakePeer

    from vs_sandbox.api import DockerSandbox

STREAM_PROVIDERS = tuple(agentshim.stream_provider_names())
ONE_SHOT_PROVIDERS = tuple(
    name for name in subject.supported_providers() if name not in STREAM_PROVIDERS
)

SECRET = "s3cret-value"  # noqa: S105  # a fixture value, not a credential
WINDOW = {"unifiedWindows": {"five_hour": {"utilization": 0.4, "resetsAt": 1_791_954_019}}}


def _answering(provider: str, *texts: str) -> Callable[[SpawnRequest], FakePeer]:
    """The far end of ``provider``'s process, answering one turn per text."""
    if provider == "claude":
        return ClaudeStreamPeers([ClaudePeerTurn(text=text) for text in texts]).build
    script = CodexScript()
    for text in texts:
        script.turn(Say(text))
    return script.peer


def _reporting_rate_limits(provider: str) -> Callable[[SpawnRequest], FakePeer]:
    """A process whose one turn reports a rate-limit window and then answers."""
    if provider == "claude":
        return ClaudeStreamPeers([ClaudePeerTurn(text="done", rate_limit=WINDOW)]).build
    return CodexScript().turn(ReportRateLimits(), Say("done")).peer


#: Providers whose fake can keep a turn running after it reported a window.
STALLING = {"codex": lambda: CodexScript().turn(ReportRateLimits(), Hang()).peer}


def _awaiting_steer(provider: str) -> Callable[[SpawnRequest], FakePeer]:
    """A process whose turn waits for an operator message, then answers STEERED."""
    if provider == "claude":
        return ClaudeStreamPeers(
            [ClaudePeerTurn(stall=True, injects_steer=True), ClaudePeerTurn(text="STEERED")]
        ).build
    return CodexScript().turn(AwaitSteer(then=(Say("STEERED"),))).peer


def test_every_stream_provider_has_a_script() -> None:
    assert STREAM_PROVIDERS == ("claude", "codex")


@dataclass
class _Observer:
    events: list[AgentEvent] = field(default_factory=list)
    seen: dict[AgentEventKind, threading.Event] = field(default_factory=dict)

    def on_event(self, event: AgentEvent) -> None:
        self.events.append(event)
        self.seen.setdefault(event.kind, threading.Event()).set()

    def wait_for(self, kind: AgentEventKind) -> None:
        self.seen.setdefault(kind, threading.Event()).wait()

    def kinds(self) -> list[AgentEventKind]:
        return [event.kind for event in self.events]


def _driver(
    provider: str, executor: FakeExecutor, sandbox: FakeDockerSandbox
) -> subject.AgentShimDriver:
    return subject.AgentShimDriver(
        provider=provider,
        docker_sandboxes={"implementer": cast("DockerSandbox", sandbox)},
        executor_factory=lambda: executor,
        launcher_env=dict,
        transient_retry_delays=(),
    )


def _container_spec(tmp_path: Path, provider: str) -> AgentSessionSpec:
    return AgentSessionSpec(
        role="implementer",
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        policy=AgentExecutionPolicy(containerized=True),
    )


@pytest.mark.parametrize("provider", STREAM_PROVIDERS)
def test_container_turns_share_one_process_marked_for_reaping(
    tmp_path: Path, provider: str
) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path, extra_env={"ANTHROPIC_AUTH_TOKEN": SECRET})
    executor = FakeExecutor([], peers=_answering(provider, "one", "two"))
    session = _driver(provider, executor, sandbox).create_session(
        _container_spec(tmp_path, provider)
    )

    first = session.run_turn(AgentTurnRequest(message="first"))
    second = session.run_turn(AgentTurnRequest(message="second"))

    assert (first.text, second.text) == ("one", "two")
    assert second.provider_session_id == first.provider_session_id
    assert len(executor.spawns) == 1
    argv = list(executor.spawns[0].argv)
    assert argv[:5] == ["docker", "exec", "-i", "-e", "AGENTSHIM_CONFINED=1"]
    assert argv[argv.index("-w") + 1] == "/workspace"
    assert sandbox.container_id in argv
    # The credential rides in the docker client's environment, never its argv.
    assert not any(SECRET in part for part in argv)
    assert executor.spawns[0].env["ANTHROPIC_AUTH_TOKEN"] == SECRET
    assert "ANTHROPIC_AUTH_TOKEN" in argv


@pytest.mark.parametrize("provider", ONE_SHOT_PROVIDERS)
def test_providers_without_a_stream_transport_stay_one_process_per_turn(
    tmp_path: Path, provider: str
) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path)
    executor = FakeExecutor(scripted_turn(provider, text="ok"))
    session = _driver(provider, executor, sandbox).create_session(
        _container_spec(tmp_path, provider)
    )

    session.run_turn(AgentTurnRequest(message="go"))
    session.run_turn(AgentTurnRequest(message="again"))

    assert executor.spawns == []
    turns = [r for r in executor.requests if "--help" not in r.argv]
    assert len(turns) == 2


class _SteerWhenRunning:
    """Offer a steer on the turn's own thread at each event, until one is taken."""

    def __init__(self, session: SteerableSession, text: str) -> None:
        self._session = session
        self._text = text
        self.outcomes: list[SteerOutcome] = []

    def on_event(self, event: AgentEvent) -> None:
        del event
        if SteerOutcome.DELIVERED not in self.outcomes:
            self.outcomes.append(self._session.steer(self._text, on_rejected=lambda: None))


@pytest.mark.parametrize("provider", STREAM_PROVIDERS)
def test_a_steer_is_delivered_to_the_running_container_turn(tmp_path: Path, provider: str) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path)
    executor = FakeExecutor([], peers=_awaiting_steer(provider))
    session = _driver(provider, executor, sandbox).create_session(
        _container_spec(tmp_path, provider)
    )
    assert isinstance(session, SteerableSession)
    steerer = _SteerWhenRunning(session, "instead, say STEERED")

    result = session.run_turn(AgentTurnRequest(message="work"), steerer)

    assert steerer.outcomes[-1] is SteerOutcome.DELIVERED
    # Only "the turn is not live yet" may come before the delivery.
    assert set(steerer.outcomes[:-1]) <= {SteerOutcome.NO_RUNNING_TURN}
    assert result.text == "STEERED"


@pytest.mark.parametrize("provider", STREAM_PROVIDERS)
def test_a_rate_limit_report_precedes_the_turns_close(tmp_path: Path, provider: str) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path)
    executor = FakeExecutor([], peers=_reporting_rate_limits(provider))
    session = _driver(provider, executor, sandbox).create_session(
        _container_spec(tmp_path, provider)
    )
    observer = _Observer()

    session.run_turn(AgentTurnRequest(message="work"), observer)

    kinds = observer.kinds()
    # The turn's closing usage report comes last; the window was reported before it.
    assert kinds[-1] is AgentEventKind.USAGE
    assert AgentEventKind.RATE_LIMIT in kinds[:-1]


@pytest.mark.parametrize("provider", sorted(STALLING))
def test_a_rate_limit_report_reaches_the_observer_while_the_turn_still_runs(
    tmp_path: Path, provider: str
) -> None:
    sandbox = FakeDockerSandbox(workspace=tmp_path)
    executor = FakeExecutor([], peers=STALLING[provider]())
    session = _driver(provider, executor, sandbox).create_session(
        _container_spec(tmp_path, provider)
    )
    observer = _Observer()
    failures: list[BaseException] = []

    def turn() -> None:
        try:
            session.run_turn(AgentTurnRequest(message="work"), observer)
        except BaseException as error:  # noqa: BLE001  # the cancelled turn raises by design
            failures.append(error)

    worker = threading.Thread(target=turn)
    worker.start()
    observer.wait_for(AgentEventKind.RATE_LIMIT)
    # The turn has not ended: the report arrived over the live process.
    assert worker.is_alive()
    session.cancel()
    worker.join()

    assert isinstance(failures[0], agentshim.TurnCancelledError)
