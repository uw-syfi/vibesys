"""Apply the quota policy when a provider has no capacity left."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_agent.api import Attribution, CapacityGated, ProviderSwitch, QuotaCondition, QuotaPlan
from vs_core.api import CapacityHold
from vs_runtime._fallback import ProviderFallback
from vs_runtime._quota_policy import (
    PROVIDER_OUTAGE,
    GiveUp,
    Hold,
    OutagePolicy,
    QuotaAction,
    QuotaPolicy,
    Switch,
    WaitFor,
    capacity_returns_at,
    capacity_wait,
    decide_outage,
    decide_quota,
    describe_seconds,
    outage_policy_for,
)
from vs_runtime._run_control import CapacityWake, PauseWake

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import NoReturn

    from vs_agent.api import (
        AgentClientProtocol,
        AgentEventSink,
        AgentQuotaError,
        AgentTurnRequest,
        CapacityStops,
        ProviderSwitchReason,
    )
    from vs_runtime._run_control import CapacityWaitEnd, RunControlChannel
    from vs_sim.api import Clock, Threads


class CapacityTimer(Protocol):
    """Time for the capacity gate: the current epoch second and a bounded park."""

    def now(self) -> float:
        """Seconds since the epoch, the timeline provider reset times are on."""
        ...

    def pause_for_quota(self, seconds: float, turn: CapacityStops) -> PauseWake:
        """Pause the run for a quota stop, resuming it ``seconds`` from now; park until it resumes.

        As :meth:`RunControlChannel.pause_for_quota` then
        :meth:`RunControlChannel.wait_resumed`: the timed resume belongs to the run, so a
        cancellation of ``turn`` ends only this turn's wait (``DETACHED``) and leaves the
        pause and its timed resume in force for the rest of the run. Under the operator's
        hold no timed resume is armed. A stop request raises.
        """
        ...

    def wait_for_capacity(
        self, seconds: float, request: int, turn: CapacityStops
    ) -> CapacityWaitEnd:
        """Wait at most ``seconds`` for capacity under the operator's ``request``.

        As :meth:`RunControlChannel.wait_for_capacity`: an elapsed wait resumes the run, and
        the operator ends it sooner by resuming, pausing, or asking again. A cancellation
        of ``turn`` ends only this turn's wait (``DETACHED``), changing nothing else.
        """
        ...

    def park_for_capacity(self, turn: CapacityStops, *, waiting: bool) -> int | PauseWake:
        """As :meth:`RunControlChannel.park_for_capacity`; a cancellation of ``turn`` ends it.

        The run stays paused for the operator when the cancellation ends the park.
        """
        ...

    def wait_out(self, turn: CapacityStops, seconds: float) -> None:
        """Hold one turn for at most ``seconds``, on this turn's thread only.

        A cancellation or an interruption of ``turn`` ends the wait sooner. The run's pause
        state is not touched: the rest of the run goes on.
        """
        ...


class ControlCapacityTimer:
    """Production timer: the wall clock, and the run control channel's own bounded park."""

    def __init__(self, control: RunControlChannel, now: Callable[[], float]) -> None:
        """Bind the timer to the channel that owns the pause and to a clock."""
        self._control = control
        self._now = now

    def now(self) -> float:
        """Seconds since the epoch."""
        return self._now()

    def pause_for_quota(self, seconds: float, turn: CapacityStops) -> PauseWake:
        """Pause the run on the control channel, then park until it resumes or ``turn`` is cancelled."""
        self._control.pause_for_quota(seconds)
        unsubscribe = turn.on_cancel(self._control.interrupt_waits)
        try:
            return self._control.wait_resumed(ended=lambda: turn.cancelled)
        finally:
            unsubscribe()

    def wait_for_capacity(
        self, seconds: float, request: int, turn: CapacityStops
    ) -> CapacityWaitEnd:
        """Wait on the control channel for at most ``seconds``, or until ``turn`` is cancelled."""
        unsubscribe = turn.on_cancel(self._control.interrupt_waits)
        try:
            return self._control.wait_for_capacity(seconds, request, ended=lambda: turn.cancelled)
        finally:
            unsubscribe()

    def park_for_capacity(self, turn: CapacityStops, *, waiting: bool) -> int | PauseWake:
        """Park on the control channel for the operator, or until ``turn`` is cancelled."""
        unsubscribe = turn.on_cancel(self._control.interrupt_waits)
        try:
            return self._control.park_for_capacity(waiting=waiting, ended=lambda: turn.cancelled)
        finally:
            unsubscribe()

    def wait_out(self, turn: CapacityStops, seconds: float) -> None:
        """Park on the turn's own event for at most ``seconds``."""
        turn.park(seconds)


type Route = tuple[str, str | None]
"""A provider and the model a turn runs on there (``None`` for the provider's default)."""


@dataclass(frozen=True, slots=True)
class RouteHealth:
    """What one route did so far this run: turns it served, and outage stops on it."""

    served: int = 0
    outage_stops: int = 0


class ProviderHealth:
    """What each route (a provider and a model) did this run, so an outage can be told apart.

    A route that is down serves no turn; one that serves other turns while a turn on it
    keeps failing, with no other turn stopping on it, is up, and that turn's failure (any
    server error is transient to agentshim) is the turn's own. A route is a provider and
    a model because one provider may serve one model while another is down. The counts
    only grow, so a gate compares a snapshot with a later one. Thread-safe; ``threads``
    makes its lock.
    """

    def __init__(self, threads: Threads) -> None:
        """Start with nothing seen on any route."""
        self._lock = threads.lock()
        self._routes: dict[Route, RouteHealth] = {}

    def record_served(self, route: Route) -> None:
        """Count one turn ``route`` served."""
        with self._lock:
            now = self._routes.get(route, RouteHealth())
            self._routes[route] = RouteHealth(now.served + 1, now.outage_stops)

    def record_outage_stop(self, route: Route) -> None:
        """Count one turn that stopped on an outage of ``route``."""
        with self._lock:
            now = self._routes.get(route, RouteHealth())
            self._routes[route] = RouteHealth(now.served, now.outage_stops + 1)

    def snapshot(self, route: Route) -> RouteHealth:
        """What ``route`` did so far."""
        with self._lock:
            return self._routes.get(route, RouteHealth())


@dataclass(slots=True)
class _OutageStreak:
    """One turn's run of consecutive outage stops; a stop on another limit ends it.

    ``since`` is when its first stop reached the gate and ``stops`` how many it has had.
    ``released`` is the route's health when the gate last let the turn go, so the next
    stop sees what the route did while that send was failing.
    """

    since: float
    stops: int = 0
    released: RouteHealth | None = None


@dataclass(slots=True)
class _TurnMemory:
    """What the gate keeps about one turn between its stops, in ``CapacityStops.memory``.

    ``quota_waited`` is the time the turn waited on named limits (quotas, rate limits)
    only, the budget ``decide_quota`` spends. ``capacity_requested`` is whether the
    operator asked this turn to wait for capacity. ``outage`` is the current outage
    streak, kept apart so a quota stop and an outage stop never spend each other's budget.

    ``held``, ``held_since`` and ``released_at`` are the turn's hold as the gate reports it
    to core (``CapacityHold``), which never charges it to the turn's limit: the length of
    the holds that ended, when the current one began (an outage streak is one hold, the
    sends between its waits included), and when its latest wait ended.
    """

    quota_waited: float = 0.0
    capacity_requested: bool = False
    outage: _OutageStreak | None = None
    held: float = 0.0
    held_since: float | None = None
    released_at: float | None = None

    def hold(self, now: float, *, continues: bool) -> None:
        """Begin a hold at ``now``, or with ``continues`` go on with the current one."""
        if not continues:
            self.close(now)
        if self.held_since is None:
            self.held_since = now
        self.released_at = None

    def close(self, now: float) -> None:
        """End the current hold, at its latest release or else ``now``, and credit it."""
        if self.held_since is not None:
            end = now if self.released_at is None else self.released_at
            self.held += end - self.held_since
        self.held_since = self.released_at = None

    def release(self, now: float) -> None:
        """End the hold's latest wait at ``now``: the turn is sent again or ends."""
        if self.held_since is not None and self.released_at is None:
            self.released_at = now

    def report(self, budget: float, *, parked: bool = False) -> CapacityHold:
        """The hold as core takes it."""
        return CapacityHold(
            credit=self.held,
            since=self.held_since,
            released_at=self.released_at,
            parked=parked and self.held_since is not None and self.released_at is None,
            budget=budget,
        )


def _memory(stops: CapacityStops) -> _TurnMemory:
    """The gate's memory of the turn ``stops`` records, made at its first stop."""
    memory = stops.memory
    if not isinstance(memory, _TurnMemory):
        memory = _TurnMemory()
        stops.memory = memory
    return memory


@dataclass(frozen=True, slots=True)
class CapacityHandling:
    """How a run handles provider capacity limits, shared by every session it opens.

    ``policy`` governs the limits a provider names (a quota, a rate limit); ``outage``
    governs a provider that keeps failing without naming one. ``fallback`` records
    which providers the run has replaced and is the one place sessions consult when
    they open; ``health`` counts the turns each provider served, across every client.
    A ``None`` timer is the production one, built from the run's control channel.
    """

    policy: QuotaPolicy
    fallback: ProviderFallback
    health: ProviderHealth
    timer: CapacityTimer | None = None
    outage: OutagePolicy = PROVIDER_OUTAGE

    @classmethod
    def for_policy(
        cls, policy: QuotaPolicy, threads: Threads, timer: CapacityTimer | None = None
    ) -> CapacityHandling:
        """Handling for ``policy``, with a fallback state holding its target.

        ``threads`` makes the locks of the fallback state and the provider health.
        """
        return cls(
            policy, ProviderFallback(policy.fallback, threads), ProviderHealth(threads), timer
        )


class PolicyCapacityGate:
    """Hold a turn stopped on a provider capacity limit until it may be sent again.

    A limit the provider names (a quota, a rate limit) pauses the run, which the policy
    or the operator ends; an outage holds only the turn (see below).

    For a named limit the run's existing cooperative pause carries the state: the turn that hit
    the limit stays in flight on its worker thread, the run reports PAUSING and
    then PAUSED, and the typed ``quota_paused`` event says why. Depending on the
    policy the run waits for the operator, resumes itself when capacity should
    have returned, or gives up and lets the turn fail with the quota error.
    After a resume the same turn is sent again, so a run that waited and resumed
    on its provider has the same experiment state as one that never stopped. A
    stop request ends the wait by raising ``RunStopped``; a cancellation of the turn
    ends that turn's wait with the quota error and nothing else: the pause, its timed
    resume (which the channel owns), the operator's request to wait for capacity and the
    other turns waiting stay as they were. Core does not count the time a quota pause holds
    the run against the limit of a turn already dispatched, so a wait that outlasts the role's
    turn time is not cut short by it.

    A provider outage (``QuotaCondition.UNAVAILABLE``) is waited out under the
    outage policy instead (:func:`outage_policy_for`: the whole budget under ``pause``
    and ``wait``, one backoff step under ``fail`` and ``fallback``), and without pausing
    the run: only the stopped turn waits, on its own thread, while the run goes on
    dispatching measurements and other turns. The turn is sent again with backoff
    until the outage budget is spent, then, under ``pause`` and ``wait``, the run
    pauses for the operator as for a quota (under ``fail`` the turn fails, under
    ``fallback`` the run switches); the run's deadline ends the wait, and a stop
    request or a cancellation of the turn ends it at once. A run left unattended
    overnight must not end on an outage (S179). A stop is not an outage
    when, while the turn's latest send was failing, its route (provider and model)
    served other turns and no other turn stopped on it: the route is up and the
    failure is the turn's own, so the turn fails at once.

    The gate reports each turn's hold (``CapacityHold``) on the control channel when it
    begins waiting and when it lets the turn go; the run loop hands the reports to core,
    which never charges held time to the turn's limit, so a hold the policy allows is
    never cut short by the limit.

    Quota stops and outage stops of one turn keep apart: an outage never spends the
    quota wait budget, and a quota stop ends the turn's outage streak, so a later
    outage starts its backoff and budget afresh.

    A switch to the fallback provider (by the policy once its wait is spent, or at
    the operator's request) is recorded in the run's ``ProviderFallback`` and ends
    the turn in flight with the quota error: a session cannot change provider, so
    that turn's hypothesis stops here and sessions opened afterwards run on the
    fallback. A session still open on the replaced provider fails its next
    capacity stop at once.
    """

    def __init__(
        self,
        control: RunControlChannel,
        events: AgentEventSink,
        handling: CapacityHandling,
        timer: CapacityTimer,
    ) -> None:
        """Bind the gate to the run's control channel, event sink, handling and timer."""
        self._control = control
        self._events = events
        self._policy = handling.policy
        self._outage = outage_policy_for(handling.policy, handling.outage)
        self._fallback = handling.fallback
        self._health = handling.health
        self._timer = timer

    def turn_served(self, provider: str, model: str | None) -> None:
        """Count a turn the route served, which shows the route is up."""
        self._health.record_served((provider, model))

    def wait_for_capacity(
        self, error: AgentQuotaError, turn: AgentTurnRequest, *, role: str, stops: CapacityStops
    ) -> None:
        """Return once the turn may be sent again; raise to end it.

        An outage holds only this turn; any other capacity limit pauses the run. The gate
        reports the turn's hold on the control channel while it waits and when it lets the
        turn go, so core never charges that time to the turn's limit.
        """
        context = Attribution(role, turn.label, turn.invocation_id)
        memory = _memory(stops)
        try:
            self._stop(error, stops, context)
        finally:
            memory.release(self._timer.now())
            self._report(context, memory)

    def _report(self, context: Attribution, memory: _TurnMemory, *, parked: bool = False) -> None:
        """Tell the run loop how long the gate has held the turn, for core to credit."""
        if context.invocation_id is not None:
            self._control.note_capacity_hold(
                context.invocation_id, memory.report(self._outage.budget_seconds, parked=parked)
            )

    def _stop(self, error: AgentQuotaError, stops: CapacityStops, context: Attribution) -> None:
        """Hold the turn through one capacity stop; raise to end it."""
        memory = _memory(stops)
        now = self._timer.now()
        if stops.cancelled:
            self._abandon(error, "the turn was cancelled", context)
        if self._fallback.replaced(error.provider):
            self._abandon(
                error,
                f"the run switched away from {error.provider}; this session cannot continue on it",
                context,
            )
        if error.condition is QuotaCondition.UNAVAILABLE:
            self._wait_out_outage(error, stops, now, context)
            return
        memory.outage = None
        memory.hold(now, continues=False)
        decision = decide_quota(
            self._policy, resets_at=error.resets_at, now=now, waited=memory.quota_waited
        )
        if isinstance(decision, GiveUp):
            self._abandon(error, decision.reason, context)
        if isinstance(decision, Switch):
            self._switch_or_abandon(error, "policy", decision.reason, context)
        resumes_at = now + decision.seconds if isinstance(decision, WaitFor) else None
        target = self._fallback.target
        plan = QuotaPlan(
            self._policy.action.value,
            resumes_at,
            None if target is None else target.provider,
            None if target is None else target.model,
        )
        memory.quota_waited += self._pause_run(error, stops, context, plan, decision)

    def _pause_run(
        self,
        error: AgentQuotaError,
        stops: CapacityStops,
        context: Attribution,
        plan: QuotaPlan,
        decision: Hold | WaitFor,
    ) -> float:
        """Pause the run for the turn's stop, park the turn, and return how long it waited.

        ``WaitFor`` arms a timed resume; ``Hold`` waits for the operator. Raises the quota
        error when the turn was cancelled or the operator resumed with the fallback.
        """
        memory = _memory(stops)
        now = self._timer.now()
        self._events.quota_paused(error, plan, context)
        self._report(context, memory, parked=True)
        timed = isinstance(decision, WaitFor)
        # The turn owns the pause from before it exists until it stops waiting on it.
        self._control.enter_capacity(timed=timed)
        try:
            if isinstance(decision, WaitFor):
                wake = self._timer.pause_for_quota(decision.seconds, stops)
            else:
                self._control.pause_for_quota()
                wake = self._hold(error, stops, memory, context)
        finally:
            self._control.leave_capacity(timed=timed)
        # The run resumed: the hold ends here, credited whole however long the run was paused.
        memory.close(self._timer.now())
        waited = self._timer.now() - now
        if wake is PauseWake.DETACHED or stops.cancelled:
            self._abandon(
                error, f"the turn was cancelled after {describe_seconds(waited)}", context, waited
            )
        if self._control.consume_fallback_request() and self._fallback.target is not None:
            self._switch_or_abandon(
                error, "operator", "the operator resumed with the fallback", context, waited
            )
        self._events.quota_resumed(
            error, "operator" if wake is PauseWake.RESUMED else "wait_elapsed", waited, context
        )
        return waited

    def _wait_out_outage(
        self, error: AgentQuotaError, stops: CapacityStops, now: float, context: Attribution
    ) -> None:
        """Hold this turn through one outage backoff, without touching the run's pause.

        Returns when the turn should be sent again. Once the budget is spent under a policy
        that waits for capacity, pauses the run for the operator instead (``_park_outage``).
        Raises ``RunStopped`` on a stop request, and the quota error when the turn was
        cancelled, the run reached its deadline, the budget is spent under ``fail``, or the
        route served other turns while this turn's latest send was failing and no other
        turn stopped on it.
        """
        route = (error.provider, stops.model)
        memory = _memory(stops)
        streak = memory.outage
        memory.hold(now, continues=streak is not None)
        if streak is None:
            streak = memory.outage = _OutageStreak(since=now)
        elif streak.released is not None and self._route_up(route, streak.released):
            self._abandon(
                error,
                f"{error.provider} served other turns on this model while this turn's last "
                "send was failing, and none of them stopped, so it is up and this failure "
                "is the turn's own",
                context,
            )
        self._health.record_outage_stop(route)
        streak.stops += 1
        deadline = self._control.run_deadline()
        decision = decide_outage(
            self._outage,
            stop=streak.stops,
            elapsed=now - streak.since,
            run_left=None if deadline is None else deadline - now,
        )
        if isinstance(decision, GiveUp):
            self._abandon(error, decision.reason, context)
        if isinstance(decision, Switch):
            self._switch_or_abandon(error, "policy", decision.reason, context)
        if isinstance(decision, Hold):
            self._park_outage(error, stops, context)
            return
        plan = QuotaPlan(QuotaAction.WAIT.value, now + decision.seconds, None, None)
        self._events.quota_paused(error, plan, context)
        self._report(context, memory)
        unsubscribe = self._control.on_stop_requested(stops.interrupt)
        try:
            if not self._control.stop_requested() and not stops.cancelled:
                self._timer.wait_out(stops, decision.seconds)
        finally:
            unsubscribe()
        waited = self._timer.now() - now
        self._control.raise_if_stopped()
        if stops.cancelled:
            self._abandon(
                error, f"the turn was cancelled after {describe_seconds(waited)}", context, waited
            )
        if deadline is not None and self._timer.now() >= deadline:
            self._abandon(
                error,
                "the run reached its deadline while the provider was unavailable",
                context,
                waited,
            )
        streak.released = self._health.snapshot(route)
        self._events.quota_resumed(error, "wait_elapsed", waited, context)

    def _park_outage(
        self, error: AgentQuotaError, stops: CapacityStops, context: Attribution
    ) -> None:
        """Pause the run for the operator: the turn waited out the whole outage budget.

        The provider may never come back (a revoked account, a removed model), which only
        the operator can tell, so the run waits for them as for a quota instead of ending
        (S179). The turn stays parked, its limit uncharged, until the operator resumes the
        run or asks to wait for capacity; then it is sent again and its next outage stop
        starts a new streak and budget. A cancellation of the turn ends only its park.
        """
        target = self._fallback.target
        plan = QuotaPlan(
            QuotaAction.PAUSE.value,
            None,
            None if target is None else target.provider,
            None if target is None else target.model,
        )
        self._pause_run(error, stops, context, plan, Hold())
        _memory(stops).outage = None

    def _route_up(self, route: Route, released: RouteHealth) -> bool:
        """Whether ``route`` served turns, and no other turn stopped on it, since ``released``."""
        now = self._health.snapshot(route)
        return now.served > released.served and now.outage_stops == released.outage_stops

    def _hold(
        self,
        error: AgentQuotaError,
        stops: CapacityStops,
        memory: _TurnMemory,
        context: Attribution,
    ) -> PauseWake:
        """Park for the operator, or until capacity returns when the operator asked to wait.

        ``RESUMED`` when the operator ended the pause, ``ELAPSED`` when capacity should be
        back or the run's timed resume came due, as the channel recorded it. A turn sent again after such a wait that stops on the limit again waits again
        by itself, polling at the policy's retry interval when the provider gives no reset
        time. A cancellation of the turn ends only its own park (``DETACHED``): the operator's
        request stays in force for the other turns parked on a capacity limit.
        """
        request = self._timer.park_for_capacity(stops, waiting=memory.capacity_requested)
        while isinstance(request, int) and not stops.cancelled:
            memory.capacity_requested = True
            now = self._timer.now()
            back = capacity_returns_at(resets_at=error.resets_at, now=now)
            self._events.quota_waiting(error.provider, back, context)
            wait = capacity_wait(self._policy, resets_at=error.resets_at, now=now)
            end = self._timer.wait_for_capacity(wait, request, stops)
            match end.wake:
                case CapacityWake.ELAPSED:
                    return PauseWake.ELAPSED
                case CapacityWake.RESUMED:
                    self._events.quota_wait_cancelled(error.provider, "resumed", context)
                    return PauseWake.RESUMED
                case CapacityWake.DETACHED:
                    return PauseWake.DETACHED
                case CapacityWake.CANCELLED:
                    self._events.quota_wait_cancelled(error.provider, "paused", context)
                    memory.capacity_requested = False
                    request = self._timer.park_for_capacity(stops, waiting=False)
                case CapacityWake.REARMED:
                    request = end.request
        return PauseWake.DETACHED if stops.cancelled or isinstance(request, int) else request

    def _switch_or_abandon(
        self,
        error: AgentQuotaError,
        reason: ProviderSwitchReason,
        detail: str,
        context: Attribution,
        waited: float = 0.0,
    ) -> NoReturn:
        """Replace the provider by the fallback when there is one, then end the turn."""
        target = self._fallback.target
        if target is None or target.provider == error.provider:
            self._abandon(error, f"no fallback to switch to ({detail})", context, waited)
        if self._fallback.replace_provider(error.provider):
            self._events.provider_switched(
                ProviderSwitch(error.provider, target.provider, target.model, reason, detail),
                context,
            )
        self._abandon(
            error,
            f"switched to {target.provider} ({target.model}); the turn cannot move to another provider",
            context,
            waited,
        )

    def _abandon(
        self, error: AgentQuotaError, reason: str, context: Attribution, waited: float = 0.0
    ) -> NoReturn:
        """End the turn in flight with the quota error; ``waited`` is how long this stop held it."""
        self._events.quota_abandoned(error, reason, waited, context)
        raise error


def install_capacity_gate(
    client: AgentClientProtocol,
    *,
    control: RunControlChannel,
    events: AgentEventSink,
    handling: CapacityHandling,
    clock: Clock,
) -> None:
    """Make ``client`` apply the run's quota policy when its provider has no capacity.

    ``clock`` is the time the production timer (the one a ``None`` ``handling.timer`` selects)
    measures waits on.

    Every client a run builds goes through here, so a quota stop pauses, waits or fails
    the same way for all of them. A client that cannot take a gate (no ``CapacityGated``
    capability) lets the quota error propagate, as a run without a policy would.
    """
    if isinstance(client, CapacityGated):
        timer = handling.timer or ControlCapacityTimer(control, clock.now)
        client.set_capacity_gate(PolicyCapacityGate(control, events, handling, timer))
