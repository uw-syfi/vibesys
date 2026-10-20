"""The capacity gate reports a turn's hold, so core's turn limit never cuts an outage wait short (S179).

Core bounds every agent turn by its role's ``turn_time`` (planner and judge 30 min, implementer
2 h) and cancels the turn when the bound passes. The gate holds a stopped turn inside that same
call. Before, nothing told core, so the hold counted against the limit: a two hour outage, well
inside the six hour outage budget, ended a planner turn at 30 min after one minute at the gate.

Now the gate reports each hold to the run's control channel (``take_capacity_holds``), and core
extends the turn's limit by it (``vs_core``'s ``CapacityHold``). Here core's limit is modelled by
a thread that reads what the channel reports and cancels the turn once its uncredited time
reaches ``turn_time``, by the rule core applies (``vs_core``'s ``held_limit``).

Once the outage outlasts its budget under the ``pause`` or ``wait`` policy, the gate pauses the
run for the operator rather than give the turn up, and the turn is sent again on resume.

Virtual time, simulated threads, the production gate and timer; the provider is the Fake one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_agent.api import (
    AgentClient,
    AgentQuotaError,
    AgentSessionKey,
    AgentTurnRequest,
    Attribution,
    NullAgentEventSink,
    QuotaCondition,
    QuotaPlan,
    SessionScope,
)
from vs_agent.api.testing import FakeProvider, FakeTurnScript
from vs_core.api import StopMode, held_limit
from vs_runtime.api.infrastructure import (
    PROVIDER_OUTAGE,
    CapacityHandling,
    QuotaAction,
    QuotaPolicy,
    RunControlChannel,
    RunControlTransition,
    RunControlTransitionKind,
    RunStopped,
    create_run_control_channel,
    install_capacity_gate,
)
from vs_runtime.api.testing import FakeRunControlEventSink
from vs_sim.api.testing import ManualClock, SimThreads, VirtualClock, examples

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_core.api import CapacityHold

START = 1_000_000.0
BUDGET = PROVIDER_OUTAGE.budget_seconds
ROLE = "dynamic-orchestrator"
INVOCATION = "inv-1"
_CORE_POLL_S = 10.0
"""How often the modelled core reads the channel; the run loop polls its controls each second."""
_POLICIES = st.sampled_from(
    [
        QuotaPolicy(QuotaAction.PAUSE),
        QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
    ]
)


def _outage() -> AgentQuotaError:
    return AgentQuotaError(
        "claude", QuotaCondition.UNAVAILABLE, "API Error: 503 No available accounts"
    )


def credited_limit(stated: float, hold: CapacityHold | None) -> float:
    """Core's rule for a turn's limit, given the latest hold the gate reported for it."""
    return stated if hold is None else held_limit(stated, hold)


class _Sink(NullAgentEventSink):
    def __init__(self) -> None:
        self.abandoned: list[str] = []

    def quota_paused(self, error: AgentQuotaError, plan: QuotaPlan, where: Attribution) -> None:
        del error, plan, where

    def quota_resumed(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, reason, waited_seconds, where

    def quota_abandoned(
        self, error: AgentQuotaError, reason: str, waited_seconds: float, where: Attribution
    ) -> None:
        del error, waited_seconds, where
        self.abandoned.append(reason)


@dataclass
class _Turn:
    """One turn under ``policy`` through an outage of ``outage`` seconds.

    The turn's first send works ``lead`` seconds before the outage starts; each failing send
    then takes ``send_s`` (agentshim's retry and the CLI's own) before it reaches the gate.
    ``provider``, when given, replaces that model: it runs each send on the turn's thread.
    """

    policy: QuotaPolicy
    outage: float
    send_s: float
    lead: float = 0.0
    provider: Callable[[_Turn], None] | None = None
    clock: VirtualClock = field(
        default_factory=lambda: VirtualClock(START, limit=START + 4 * BUDGET)
    )
    transitions: list[RunControlTransition] = field(default_factory=list)
    sink: _Sink = field(default_factory=_Sink)
    ended: list[str] = field(default_factory=list)
    holds: list[CapacityHold] = field(default_factory=list)
    cut_at: list[float] = field(default_factory=list)
    """When the modelled core cancelled the turn at its limit (the Fake send runs on regardless)."""
    threads: SimThreads = field(init=False)
    control: RunControlChannel = field(init=False)
    client: AgentClient = field(init=False)

    def __post_init__(self) -> None:
        self.threads = SimThreads(self.clock)
        self.control = create_run_control_channel(
            FakeRunControlEventSink(on_transition=self.transitions.append), threads=self.threads
        )

        def send(request: AgentTurnRequest) -> None:
            del request
            if self.provider is not None:
                self.provider(self)
                return
            self.threads.sleep(max(START + self.lead - self.clock.now(), 0.0) + self.send_s)
            if self.clock.now() < START + self.lead + self.outage:
                raise _outage()

        self.client = AgentClient(
            FakeProvider(script=FakeTurnScript(("plan",)), on_turn=send, threads=self.threads),
            provider="claude",
            model_name="m",
            clock=ManualClock(START),
            threads=self.threads,
        )
        install_capacity_gate(
            self.client,
            control=self.control,
            events=self.sink,
            handling=CapacityHandling.for_policy(self.policy, self.threads),
            clock=self.clock,
        )

    def latest(self) -> CapacityHold | None:
        """The latest hold the channel reported for the turn (what the bridge gives core)."""
        reported = self.control.take_capacity_holds().get(INVOCATION)
        if reported is not None:
            self.holds.append(reported)
        return self.holds[-1] if self.holds else None

    def play(self, turn_time: float, *, operator_resumes_after: float | None = None) -> None:
        """Run the turn; the operator resumes a paused run ``operator_resumes_after`` s later."""

        def core_turn_limit() -> None:
            paused_at: float | None = None
            while not self.ended:
                now = self.clock.now()
                if now >= credited_limit(START + turn_time, self.latest()):
                    self.cut_at.append(now)
                    self.client.cancel_session(AgentSessionKey(SessionScope.ROLE, ROLE))
                    return
                if operator_resumes_after is not None and self.control.pause_requested():
                    paused_at = now if paused_at is None else paused_at
                    if now - paused_at >= operator_resumes_after:
                        self.control.resume()
                        paused_at = None
                self.threads.sleep(_CORE_POLL_S)

        def main() -> None:
            self.threads.spawn(core_turn_limit, name="turn-limit", daemon=True)
            try:
                self.ended.append(
                    self.client.invoke_text(
                        kind=ROLE,
                        workspace=Path(),
                        system_prompt="Plan.",
                        user_prompt="plan",
                        round_label="dynamic-orchestrator-session-turn",
                        invocation_id=INVOCATION,
                    )
                )
            except (RuntimeError, TimeoutError, RunStopped) as error:
                self.ended.append(f"failed: {type(error).__name__}")

        self.threads.run(main)
        self.latest()  # the report the gate made as it let the turn go


@pytest.mark.regression
@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
@example(
    turn_time=1800.0,
    outage=2 * 3600.0,
    send_s=60.0,
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@given(
    turn_time=st.sampled_from([1800.0, 7200.0]),  # planner and judge, implementer
    outage=st.floats(min_value=60.0, max_value=BUDGET - 3600.0),
    send_s=st.floats(min_value=1.0, max_value=600.0),
    policy=_POLICIES,
)
def test_an_outage_shorter_than_the_budget_never_fails_a_turn(
    turn_time: float, outage: float, send_s: float, policy: QuotaPolicy
) -> None:
    """S179's shape: before, a 2 h outage ended the planner turn at its 30 min limit."""
    turn = _Turn(policy, outage, send_s)
    turn.play(turn_time)

    assert turn.ended == ["plan"]
    assert turn.cut_at == []
    assert turn.sink.abandoned == []
    if outage <= send_s:  # the first send outlasted the outage: the turn never stopped
        assert turn.holds == []
        return
    # The hold spans the turn's stay at the gate: from its first stop to its last release.
    final = turn.holds[-1]
    assert final.since == pytest.approx(START + send_s)
    assert final.released_at is not None
    assert final.released_at >= START + outage - send_s


@pytest.mark.regression
@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
# A turn resumed near its limit (review of #3283, MED-1): 300 s and 200 s of own time left.
@example(
    turn_time=1800.0,
    spare=60.0,
    outage=3600.0,
    send_s=240.0,
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@example(turn_time=1800.0, spare=50.0, outage=3600.0, send_s=150.0, policy=QuotaPolicy())
@given(
    turn_time=st.sampled_from([1800.0, 7200.0]),
    spare=st.floats(min_value=0.0, max_value=1800.0),
    outage=st.floats(min_value=60.0, max_value=BUDGET - 3600.0),
    send_s=st.floats(min_value=1.0, max_value=600.0),
    policy=_POLICIES,
)
def test_an_outage_late_in_a_turn_never_fails_it(
    turn_time: float, spare: float, outage: float, send_s: float, policy: QuotaPolicy
) -> None:
    """Wherever in the turn the outage starts, its own time left is never cut by a resend.

    ``spare`` is the own time the turn has left once its first failing send reaches the gate
    (so ``send_s + spare`` when the outage starts). Before, core credited a resend only once
    the next refusal reopened the hold, so a turn with less own time left than one send was
    cut during the resend as ``TRANSPORT_LOST``; a resumed turn is then never asked again.
    """
    left = min(send_s + spare, turn_time)
    turn = _Turn(policy, outage, send_s, lead=turn_time - left)
    turn.play(turn_time)

    assert turn.cut_at == []
    assert turn.ended == ["plan"]
    assert turn.sink.abandoned == []


@pytest.mark.regression
# Past the budget by more than one backoff and send, so the budget is spent while it lasts.
@example(outage_past=3600.0)  # S179's configuration, an outage one hour past the budget
@given(outage_past=st.floats(min_value=1800.0, max_value=3 * 3600.0))
def test_under_wait_a_spent_outage_budget_resumes_the_run_by_itself(outage_past: float) -> None:
    """``wait`` is documented to resume the run by itself: with no operator, a spent outage
    budget pauses the run on a timed resume, and the turn ends once the provider is back.

    Before (review of #3283, MED-3), the run paused for the operator, as under ``pause``, and an
    unattended run made no progress after the provider came back.
    """
    policy = QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0)
    turn = _Turn(policy, outage=BUDGET + outage_past, send_s=60.0)
    turn.play(1800.0)

    assert turn.ended == ["plan"]
    assert turn.cut_at == []
    assert turn.sink.abandoned == []
    kinds = [t.kind for t in turn.transitions]
    assert RunControlTransitionKind.PAUSED in kinds
    assert RunControlTransitionKind.RESUMED in kinds
    # Back within one timed resume (and the sends around it) of the provider's return.
    assert turn.clock.now() <= START + BUDGET + outage_past + 2 * policy.retry_seconds + 600.0


def test_under_pause_a_spent_outage_budget_waits_for_the_operator() -> None:
    """``pause`` keeps waiting for the operator once the outage budget is spent: no timed
    resume, the turn stays held however long the provider has been back."""
    turn = _Turn(QuotaPolicy(QuotaAction.PAUSE), outage=BUDGET + 3600.0, send_s=60.0)
    stop_at = START + 3 * BUDGET

    def operator_never_comes() -> None:
        turn.threads.sleep(stop_at - START)
        turn.control.request_stop(StopMode.DRAIN)

    turn.threads.spawn(operator_never_comes, name="late-stop", daemon=True)
    turn.play(1800.0)

    assert turn.ended == ["failed: RunStopped"]
    assert turn.cut_at == []
    kinds = [t.kind for t in turn.transitions]
    assert RunControlTransitionKind.PAUSED in kinds
    assert RunControlTransitionKind.RESUMED not in kinds
    assert turn.clock.now() == pytest.approx(stop_at)


@pytest.mark.parametrize("operator_after", [0.0, 2 * 3600.0], ids=["at-once", "hours-later"])
@pytest.mark.parametrize("action", [QuotaAction.PAUSE, QuotaAction.WAIT], ids=str)
def test_an_outage_past_its_budget_pauses_the_run_until_the_operator_resumes(
    action: QuotaAction, operator_after: float
) -> None:
    """A spent budget no longer gives the turn up: the run pauses, and on resume it is sent
    again. Before, the gate abandoned the turn and the strategy saw a failed turn. However
    long the operator takes, the pause is credited whole, so the resumed send is not cut."""
    turn = _Turn(QuotaPolicy(action), outage=BUDGET + 3600.0, send_s=60.0)
    turn.play(1800.0, operator_resumes_after=operator_after)

    assert turn.ended == ["plan"]
    assert turn.cut_at == []
    assert turn.sink.abandoned == []
    assert RunControlTransitionKind.PAUSED in [t.kind for t in turn.transitions]
    assert any(hold.parked for hold in turn.holds)


_OPERATOR_AFTER_S = 600.0
"""How long the operator takes to resume a run paused under ``pause``."""


@pytest.mark.regression
@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
# The review's reproduction: a 30 min turn whose first failing send (240 s) reaches the gate
# with 60 s of own time left, through an outage one hour past the budget.
@example(spare=60.0, send_s=240.0, policy=QuotaPolicy(QuotaAction.PAUSE))
@example(
    spare=60.0,
    send_s=240.0,
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@given(
    spare=st.floats(min_value=0.0, max_value=600.0),
    send_s=st.floats(min_value=1.0, max_value=600.0),
    policy=_POLICIES,
)
def test_an_outage_past_its_budget_late_in_a_turn_never_fails_it(
    spare: float, send_s: float, policy: QuotaPolicy
) -> None:
    """A late outage that outlasts the budget: the run pauses, resumes, and the turn is answered.

    ``spare`` is the own time the turn has left when its first failing send reaches the gate.
    The park closes the hold at the resume. Before (review of #3283, NEW-1), the failing resend
    after the resume opened a new hold, which began past the turn's own time; core took it as
    late, credited nothing, and cut the turn as ``TRANSPORT_LOST``. Under ``wait`` the timed
    resume is a probe, so for a long outage that resend almost always fails.
    """
    turn_time = 1800.0
    left = min(send_s + spare, turn_time)
    turn = _Turn(policy, BUDGET + 3600.0, send_s, lead=turn_time - left)
    resumes = _OPERATOR_AFTER_S if policy.action is QuotaAction.PAUSE else None
    turn.play(turn_time, operator_resumes_after=resumes)

    assert turn.cut_at == []
    assert turn.ended == ["plan"]
    assert turn.sink.abandoned == []
    assert RunControlTransitionKind.PAUSED in [t.kind for t in turn.transitions]


@dataclass
class _Windows:
    """A provider down in ``windows`` (seconds after the turn starts) and up between them.

    A send ending in a window fails after ``send_s``. A send that starts while the provider is up
    does real work until the next window opens, then fails ``send_s`` into it, as a send the
    outage interrupts does. After the last window the agent hangs, so only core's limit ends
    the turn. ``sends`` records each send's start and, for a failed one, its stop.
    """

    windows: tuple[tuple[float, float], ...]
    send_s: float
    sends: list[tuple[float, bool, float | None]] = field(default_factory=list)
    """Each send's start, whether the provider served it, and when it stopped (if it failed)."""

    def __call__(self, turn: _Turn) -> None:
        begun = turn.clock.now()
        into = begun - START
        ends = into + self.send_s
        served = not self._down(ends)
        if served:
            later = [lo for lo, _ in self.windows if lo > into]
            ends = min(later) + self.send_s if later else 3 * BUDGET  # inside the clock limit
        turn.threads.sleep(START + ends - begun)
        if self._down(turn.clock.now() - START):
            self.sends.append((begun, served, turn.clock.now()))
            raise _outage()
        self.sends.append((begun, served, None))

    def _down(self, into: float) -> bool:
        return any(lo <= into < hi for lo, hi in self.windows)

    def held(self, now: float) -> float:
        """The time the gate held the turn: from each stop that follows a served send to the
        start of the next served send (one still open at ``now`` counts up to ``now``)."""
        total, since = 0.0, None
        for begun, served, stopped in self.sends:
            if served and since is not None:
                total += begun - since
                since = None
            if stopped is not None and since is None:
                since = stopped
        return total + (0.0 if since is None else now - since)


@pytest.mark.regression
@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@example(work=5400.0, length=600.0, send_s=240.0)  # the review's reproduction
@given(
    work=st.floats(min_value=1200.0, max_value=5400.0),
    length=st.floats(min_value=300.0, max_value=1800.0),
    send_s=st.floats(min_value=30.0, max_value=300.0),
)
def test_work_between_two_outages_is_charged_to_the_turn(
    work: float, length: float, send_s: float
) -> None:
    """A 2 h implementer turn works, meets an outage, works ``work`` seconds, meets a second
    outage that ends at its stated limit, and then hangs: core cuts it at its limit plus only
    the time the gate actually held it (or the grace of its last resend, if that is later).

    Before (review of #3283, NEW-2), the hold of the first outage stayed open through the work
    that followed, and that work counted as a failing send that sized the resend grace: the
    review's turn was cut at 18 960 s, 2.4 times its limit, instead of 8 580 s.
    """
    turn_time = 7200.0
    second = turn_time - length
    first = second - work - length
    provider = _Windows(((first, first + length), (second, turn_time)), send_s)
    turn = _Turn(QuotaPolicy(QuotaAction.PAUSE), 0.0, send_s, provider=provider)
    turn.play(turn_time)

    [cut] = turn.cut_at
    # The limit plus the time held, or the resend grace of the send that got through last.
    last_release = max(begun for begun, served, _ in provider.sends if served)
    limit = max(
        START + turn_time + provider.held(cut),
        last_release + PROVIDER_OUTAGE.resend_grace_seconds,
    )
    assert limit <= cut <= limit + _CORE_POLL_S


@dataclass
class _GrowingSends:
    """An outage whose failing sends take ``first`` and then ``resends`` seconds (the last repeats).

    The turn's first send works until the outage starts, ``lead`` seconds in, then fails after
    ``first``; each resend fails after its own duration until the outage is over.
    """

    lead: float
    outage: float
    first: float
    resends: tuple[float, ...]
    sent: int = 0

    def __call__(self, turn: _Turn) -> None:
        if self.sent == 0:
            took = self.lead + self.first
        else:
            took = self.resends[min(self.sent - 1, len(self.resends) - 1)]
        self.sent += 1
        turn.threads.sleep(took)
        if turn.clock.now() < START + self.lead + self.outage:
            raise _outage()


def _resends_within_their_grace(grace: float) -> st.SearchStrategy[tuple[float, ...]]:
    """Resend durations each at most the grace it gets: ``grace``, or twice the longest before."""

    def grow(factors: list[float]) -> tuple[float, ...]:
        sends: list[float] = []
        for factor in factors:
            sends.append(factor * max(grace, 2 * max(sends, default=0.0)))
        return tuple(sends)

    return st.lists(st.floats(min_value=0.05, max_value=1.0), min_size=1, max_size=5).map(grow)


@pytest.mark.regression
@settings(max_examples=examples(pr=10, nightly=60), deadline=None)
# Resends that grow past the base grace (600 s): each fits twice the longest before it.
@example(
    spare=0.0,
    first=240.0,
    resends=(450.0, 850.0, 1600.0),
    policy=QuotaPolicy(QuotaAction.WAIT, wait_seconds=7200.0, retry_seconds=300.0),
)
@given(
    spare=st.floats(min_value=0.0, max_value=600.0),
    first=st.floats(min_value=1.0, max_value=600.0),
    resends=_resends_within_their_grace(PROVIDER_OUTAGE.resend_grace_seconds),
    policy=_POLICIES,
)
def test_a_resend_slower_than_the_base_grace_is_covered_by_twice_the_longest_send(
    spare: float, first: float, resends: tuple[float, ...], policy: QuotaPolicy
) -> None:
    """A resend gets ``max(resend_grace, 2 x the longest resend so far)`` past its release.

    So a provider whose failing sends grow (a CLI that retries longer under load) does not cut a
    turn near its limit while a resend no longer than twice the longest before it is failing.
    Before, no test had a send longer than the base grace, so dropping the doubling went
    unnoticed (review of #3283, mutation M4).
    """
    turn_time = 1800.0
    lead = turn_time - min(first + spare, turn_time)
    outage = 3 * 3600.0
    provider = _GrowingSends(lead, outage, first, resends)
    turn = _Turn(policy, outage, first, lead=lead, provider=provider)
    turn.play(turn_time)

    assert turn.cut_at == []
    assert turn.ended == ["plan"]
    assert turn.sink.abandoned == []
