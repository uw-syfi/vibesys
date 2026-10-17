"""The operator keeps a quota-paused run paused until capacity returns, then it resumes itself.

``resume_when_capacity_returns`` (``command.resume {when: capacity_returns}``) arms the same
wait the ``wait`` policy uses: the provider's reset time plus a margin, or the policy's retry
interval when the provider gave none. The operator ends the wait sooner by resuming, cancels
it by pausing, and replaces it by asking again. The operator is played from the channel's own
PAUSED transition and from inside the virtual capacity timer, so nothing sleeps.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_agent.api import AgentQuotaError, Attribution, NullAgentEventSink, QuotaCondition
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api.infrastructure import (
    RESET_MARGIN_SECONDS,
    CapacityHandling,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeCapacityTimer, FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable

ROLE = "worker"
START = 1_000_000.0
RESET = START + 600.0


class _Sink(NullAgentEventSink):
    """Remembers the quota events, in order, as ``(kind, detail)``."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    def quota_waiting(self, provider: str, resumes_at: float | None, where: Attribution) -> None:
        del provider, where
        self.events.append(("waiting", resumes_at))

    def quota_wait_cancelled(self, provider: str, reason: str, where: Attribution) -> None:
        del provider, where
        self.events.append(("cancelled", reason))

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, waited_seconds, where
        self.events.append(("resumed", reason))


type _Operator = Callable[[RunControlChannel], bool]
"""One operator action: it returns whether it did anything."""


def _wait(control: RunControlChannel) -> bool:
    return control.resume_when_capacity_returns()


def _resume(control: RunControlChannel) -> bool:
    control.resume()
    return True


def _pause(control: RunControlChannel) -> bool:
    control.request_pause()
    return True


def _idle(control: RunControlChannel) -> bool:
    del control
    return False


@dataclass
class _Run:
    """The operator's actions at each PAUSED and during each capacity wait, and what happened."""

    at_paused: list[_Operator]
    during_wait: list[_Operator] = field(default_factory=list)
    policy: QuotaPolicy = field(default_factory=QuotaPolicy)
    sink: _Sink = field(default_factory=_Sink)
    transitions: list[RunControlTransitionKind] = field(default_factory=list)
    accepted: list[bool] = field(default_factory=list)
    timer: FakeCapacityTimer | None = None
    control: RunControlChannel | None = None
    production_timer: bool = False

    def _on_transition(self, transition: RunControlTransition) -> None:
        self.transitions.append(transition.kind)
        if transition.kind is RunControlTransitionKind.PAUSED and self.at_paused:
            assert self.control is not None
            self.accepted.append(self.at_paused.pop(0)(self.control))

    def _during(self, seconds: float) -> bool:
        del seconds
        if not self.during_wait:
            return False
        assert self.control is not None
        return self.during_wait.pop(0)(self.control)

    def play(self, stops: int, resets_at: float | None) -> int:
        error = AgentQuotaError("claude", QuotaCondition.QUOTA_EXHAUSTED, "limit", resets_at)
        client = FakeAgentClient(threads=SimThreads()).fail(ROLE, error, times=stops)
        threads = client.threads
        assert isinstance(threads, SimThreads)
        control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self._on_transition), threads=threads
        )
        self.control = control
        if not self.production_timer:
            self.timer = FakeCapacityTimer(control, start=START, during_capacity_wait=self._during)
        install_capacity_gate(
            client,
            control=control,
            events=self.sink,
            handling=CapacityHandling.for_policy(self.policy, threads, self.timer),
            clock=ManualClock(START),
        )
        threads.run(
            lambda: client.invoke_text(
                kind=ROLE,
                workspace=Path(),
                system_prompt="Work.",
                user_prompt="one",
                round_label="round",
            )
        )
        return len(client.calls_for(ROLE))


def test_a_wait_for_capacity_resumes_the_run_at_the_providers_reset() -> None:
    run = _Run(at_paused=[_wait])

    calls = run.play(stops=1, resets_at=RESET)

    assert calls == 1  # the held turn is sent again, not issued anew
    assert run.accepted == [True]
    assert run.timer is not None
    assert run.timer.capacity_waits == [RESET + RESET_MARGIN_SECONDS - START]
    assert run.sink.events == [
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("resumed", "wait_elapsed"),
    ]
    assert run.transitions == [
        RunControlTransitionKind.PAUSE_REQUESTED,
        RunControlTransitionKind.PAUSED,
        RunControlTransitionKind.RESUMED,
    ]


def test_resuming_now_ends_the_wait_at_once() -> None:
    run = _Run(at_paused=[_wait], during_wait=[_resume])

    run.play(stops=1, resets_at=RESET)

    assert run.sink.events == [
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("cancelled", "resumed"),
        ("resumed", "operator"),
    ]


def test_pausing_cancels_the_wait_and_the_run_waits_for_the_operator() -> None:
    run = _Run(at_paused=[_wait, _resume], during_wait=[_pause])

    run.play(stops=1, resets_at=RESET)

    assert run.sink.events == [
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("cancelled", "paused"),
        ("resumed", "operator"),
    ]
    assert run.transitions.count(RunControlTransitionKind.PAUSED) == 2


def test_a_second_request_replaces_the_first() -> None:
    run = _Run(at_paused=[_wait], during_wait=[_wait, _idle])

    run.play(stops=1, resets_at=RESET)

    assert run.timer is not None
    assert len(run.timer.capacity_waits) == 2
    assert run.sink.events == [
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("resumed", "wait_elapsed"),
    ]


def test_without_a_reset_time_the_run_polls_at_the_retry_interval_until_it_goes_through() -> None:
    policy = QuotaPolicy(retry_seconds=120.0)
    run = _Run(at_paused=[_wait], policy=policy)

    calls = run.play(stops=3, resets_at=None)

    assert calls == 1
    assert run.accepted == [True]
    assert run.timer is not None
    assert run.timer.capacity_waits == [120.0, 120.0, 120.0]
    assert run.sink.events == [("waiting", None), ("resumed", "wait_elapsed")] * 3


def test_a_run_not_parked_on_a_capacity_limit_refuses_to_wait_for_capacity() -> None:
    threads = SimThreads()
    control = create_run_control_channel(FakeRunControlEventSink(), threads=threads)
    control.request_pause()

    assert not control.resume_when_capacity_returns()


@given(
    actions=st.lists(st.sampled_from(["resume", "pause", "wait", "idle"]), max_size=5),
)
def test_any_operator_sequence_ends_the_pause_once_with_consistent_events(
    actions: list[str],
) -> None:
    """However the operator acts during waits, each wait ends once and the turn resumes once."""
    by_name: dict[str, _Operator] = {
        "resume": _resume,
        "pause": _pause,
        "wait": _wait,
        "idle": _idle,
    }
    run = _Run(at_paused=[_wait] * 8, during_wait=[by_name[name] for name in actions])

    run.play(stops=1, resets_at=RESET)

    assert run.sink.events == _expected(actions)


def _expected(actions: list[str]) -> list[tuple[str, object]]:
    """The events the operator's actions during waits produce, by the documented rules."""
    back = RESET + RESET_MARGIN_SECONDS
    events: list[tuple[str, object]] = [("waiting", back)]
    for action in actions:
        if action == "resume":
            return [*events, ("cancelled", "resumed"), ("resumed", "operator")]
        if action == "idle":
            return [*events, ("resumed", "wait_elapsed")]
        if action == "pause":
            # paused again for the operator, who (at PAUSED) asks to wait once more
            events += [("cancelled", "paused"), ("waiting", back)]
        else:
            events.append(("waiting", back))
    return [*events, ("resumed", "wait_elapsed")]


def test_the_channels_own_timed_wait_resumes_the_run_when_it_elapses() -> None:
    """The production timer waits on the control channel, here on simulated threads."""
    run = _Run(at_paused=[_wait], production_timer=True)

    run.play(stops=1, resets_at=RESET)

    assert run.sink.events == [
        ("waiting", RESET + RESET_MARGIN_SECONDS),
        ("resumed", "wait_elapsed"),
    ]
