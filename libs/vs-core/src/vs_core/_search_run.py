"""Run a search against core: deliver events one at a time, submit what it decides.

The shell hands this module the events that happened; it calls the driver's ``update`` once
per event, submits the decisions with the core step the shell supplies, and keeps feeding the
events those steps publish until the search has nothing more to say. Everything happens
inside one call, so the shell commits search state, core state and published events
together. A ``WakeAt`` is not submitted to core: it is returned for the shell to wait on.

Also here: the runtime check that backs up the type checker for what a search returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

from ._registry import ContractError
from ._run_park import drain_bound
from ._search_events import run_view, translate
from ._validation import takes_command
from .types.common import THIS_RUN, RunStatus, Scope
from .types.kernel import ProposalSubmitted
from .types.records import RecoveryPhase
from .types.strategy import Finish, Wait, WakeAt
from .types.until import Within

if TYPE_CHECKING:
    from collections.abc import Callable

    from ._registry import OperationRegistry
    from .types.common import RunFacts, Seconds
    from .types.kernel import CoreEvent, CoreState, StrategyEvent, Transition
    from .types.search import Discarded, Event, RunView
    from .types.strategy import Decision, SearchDeclaration, StrategyState, Submission

type CoreStep = Callable[[CoreState, CoreEvent], Transition]
"""The core step the shell uses (production or a fake of it)."""


def deferred(core: CoreState, decision: Decision) -> bool:
    """Whether core cannot take ``decision`` yet but will later, so the shell holds it.

    Until recovery is ready core takes no decision. While the operator has the run paused it
    takes only what a paused run accepts (``takes_command``): the rest waits for the resume
    instead of reaching the search as a refusal, which would end the search's work.
    """
    if isinstance(decision, WakeAt):
        return False  # the shell waits on a wake; it is never submitted
    if core.records.recovery.phase != RecoveryPhase.READY:
        return True
    command = decision.request if isinstance(decision, Wait) else decision
    return (
        core.run.status in (RunStatus.DRAINING, RunStatus.PARKED)
        and drain_bound(core, command) is None
    ) or (core.run.status == RunStatus.PAUSED and not takes_command(core.run, command))


def check_decisions(decisions: object) -> tuple[Decision, ...]:
    """The runtime backstop for what a driver returned (the type checker is the first line).

    A decision is a ``Wait`` with a ``Within``, a ``WakeAt`` or a ``Finish``. A bare request, a
    bare watch, a bare number or a wait without a limit is refused with the path of the
    offending item.
    """
    if not isinstance(decisions, list | tuple):
        raise ContractError(("decisions",), "a search returns a list of decisions")
    checked: list[Decision] = []
    for index, decision in enumerate(decisions):
        path = ("decisions", index)
        match decision:
            case Wait():
                if not isinstance(decision.until, Within):
                    raise ContractError((*path, "until"), "a wait needs a Within, not a bare watch")
            case WakeAt() | Finish():
                pass
            case _:
                raise ContractError(
                    path, "a search returns Wait, WakeAt or Finish; start work inside a Wait"
                )
        checked.append(decision)
    return tuple(checked)


def _in_scope[D: Wait | Finish](decision: D, own: Scope) -> D:
    """``decision`` with the run's own scope where the search wrote ``THIS_RUN``."""
    if isinstance(decision, Wait):
        if decision.request.scope != THIS_RUN:
            return decision
        request = decision.request.model_copy(update={"scope": own})
        return decision.model_copy(update={"request": request})  # type: ignore[return-value]
    return decision.model_copy(update={"scope": own}) if decision.scope == THIS_RUN else decision


class SearchDriver[S: StrategyState, C](Protocol):
    """What the shell runs a search with: core's private mechanism, not an author's surface.

    ``flows.SearchDef.driver`` builds the one implementation. ``update`` is called once per
    event with the run's read-only view and returns what the search decided.
    """

    @property
    def declaration(self) -> SearchDeclaration:
        """What the host must provide for the search."""
        ...

    @property
    def state_model(self) -> type[S]:
        """The class of the persisted state."""
        ...

    def init(self, config: C, facts: RunFacts, view: RunView) -> tuple[S, list[Decision]]:
        """Start the search."""
        ...

    def update(self, state: S, event: Event, view: RunView) -> tuple[S, list[Decision]]:
        """Deliver one event."""
        ...

    def awaits(self, state: S, decision: Decision) -> bool:
        """Whether the search in ``state`` still waits for the answer to ``decision``.

        False for a decision of an incarnation of the search that a restart replaced: it asks
        again for what it needs, so the old decision, held or not, no longer stands.
        """
        ...

    def changes(self, before: S, after: S) -> bytes | None:
        """What ``after`` adds to ``before``, for the log; None when it does not continue it."""
        ...

    def extend(self, state: S, changes: bytes) -> S:
        """``state`` with ``changes`` applied, as a process that reads the log builds it."""
        ...

    def resume(self, state: S) -> S:
        """``state`` as the process that logged it left it, rebuilding what the log does not keep."""
        ...


@dataclass(frozen=True)
class Inbox:
    """What waits for the search: events to deliver, and decisions core could not take yet.

    A held decision is one core could not take when the search made it (``deferred``).

    ``discarded`` are events core already kept from the search (see ``translate``); they
    are passed on in ``Advanced.discarded`` so the shell logs everything in one place.
    """

    events: tuple[Event, ...] = ()
    held: tuple[Decision, ...] = ()
    discarded: tuple[Discarded, ...] = ()


@dataclass(frozen=True)
class Advanced[S: StrategyState]:
    """What one call changed: the search state, core, the events core published, the wakes.

    ``held`` are decisions the search made while core could not take them (``deferred``:
    recovery is not ready, or the run is paused); the shell keeps them and passes them back
    in the next ``Inbox``.
    """

    state: S
    core: CoreState
    discarded: tuple[Discarded, ...] = field(default=())
    published: tuple[StrategyEvent, ...] = field(default=())
    wakes: tuple[Seconds, ...] = field(default=())
    submitted: tuple[Submission, ...] = field(default=())
    held: tuple[Decision, ...] = field(default=())


@dataclass(frozen=True)
class SearchHost:
    """What the shell lends the search loop: core's step and the operation codec."""

    step: CoreStep
    registry: OperationRegistry


@dataclass
class _Pass:
    """One call's working set: events waiting for the search, and what the call produced."""

    host: SearchHost
    queue: list[Event]
    published: list[StrategyEvent] = field(default_factory=list)
    wakes: list[Seconds] = field(default_factory=list)
    sent: list[Submission] = field(default_factory=list)
    held: list[Decision] = field(default_factory=list)
    discarded: list[Discarded] = field(default_factory=list)

    def submit(self, decisions: tuple[Decision, ...], core: CoreState) -> CoreState:
        """Send the decisions to core, holding those core cannot take yet (``deferred``)."""
        self.wakes.extend(d.time for d in decisions if isinstance(d, WakeAt))
        work = tuple(d for d in decisions if isinstance(d, Wait | Finish))
        if not work or core.run.status == RunStatus.TERMINAL:
            return core
        self.held.extend(d for d in work if deferred(core, d))
        work = tuple(d for d in work if not deferred(core, d))
        if not work:
            return core
        own = Scope(owner=core.run.run_id, generation=core.run.generation)
        submissions = tuple(self.host.registry.validate_submission(_in_scope(d, own)) for d in work)
        self.sent.extend(submissions)
        transition = self.host.step(
            core, ProposalSubmitted(decisions=submissions, expected_revision=core.revision)
        )
        self.published.extend(transition.events)
        translated = translate(transition.state, transition.events)
        self.queue.extend(translated.events)
        self.discarded.extend(translated.discarded)
        return transition.state

    def drain[S: StrategyState, C](
        self, search: SearchDriver[S, C], state: S, core: CoreState
    ) -> Advanced[S]:
        """Deliver queued events one at a time until the search has nothing more to say."""
        while self.queue:
            event = self.queue.pop(0)
            state, decisions = search.update(state, event, run_view(core))
            core = self.submit(check_decisions(decisions), core)
        return Advanced(
            state=state,
            core=core,
            discarded=tuple(self.discarded),
            published=tuple(self.published),
            wakes=tuple(self.wakes),
            submitted=tuple(self.sent),
            held=tuple(self.held),
        )


def advance[S: StrategyState, C](
    search: SearchDriver[S, C], state: S, core: CoreState, host: SearchHost, inbox: Inbox
) -> Advanced[S]:
    """Submit what was held, deliver the events one by one, and feed back what that causes."""
    run = _Pass(host, list(inbox.events), discarded=list(inbox.discarded))
    core = run.submit(inbox.held, core)
    return run.drain(search, state, core)


def begin[S: StrategyState, C](
    search: SearchDriver[S, C],
    config: C,
    core: CoreState,
    host: SearchHost,
    arrived: Inbox | None = None,
) -> Advanced[S]:
    """Start a new run: ``init`` on the run's facts, submit its decisions, then ``arrived``."""
    state, decisions = search.init(config, core.run.facts, run_view(core))
    held = check_decisions(decisions)
    return advance(search, state, core, host, replace(arrived or Inbox(), held=held))
