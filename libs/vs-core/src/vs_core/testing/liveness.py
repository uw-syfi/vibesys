"""Liveness invariants every dynamic-run scenario checks: progress, not only crash safety.

A run is live when it reaches a terminal status after a bounded amount of work and
leaves nothing behind. :func:`liveness` reads a :class:`Journal` (the requests core
issued and the observations that came back, in order) and the final core state, and
returns one :class:`Violation` per broken invariant:

- ``UNBOUNDED_REQUESTS``: the run issued more requests than its own work justifies.
  The bound is derived from what the scenario did (attempts, turns, measurements and
  the retry limit), never from one constant.
- ``SPIN``: a request of the same kind and target was issued again after the earlier
  one was answered conclusively, with no new observation in between. Re-asking
  without new information cannot change the answer.
- ``NOT_TERMINAL``: the run is quiescent but its status is not terminal (unless an operator
  stop interrupted it, see :class:`End`).
- ``OPEN_INTENT``: an intent is still prepared, dispatched or reconciling.
- ``ORPHAN_WAIT``: something waits for a producer that does not exist.

A failure names the invariant and prints the offending request sequence.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core._waits import orphan_waits, phase_waits
from vs_core.types.common import Observation, RunStatus
from vs_core.types.intents import Intent, RequestObserved

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_core.types.evaluation import EvidenceRef
    from vs_core.types.intents import Request
    from vs_core.types.job_observations import MeasurementFailure
    from vs_core.types.kernel import CoreState


class Invariant(StrEnum):
    """One liveness invariant."""

    UNBOUNDED_REQUESTS = "unbounded_requests"
    SPIN = "spin"
    NOT_TERMINAL = "not_terminal"
    OPEN_INTENT = "open_intent"
    ORPHAN_WAIT = "orphan_wait"


class LivenessViolationError(AssertionError):
    """A run broke at least one liveness invariant; the message names each and the requests."""


@dataclass(frozen=True, slots=True)
class Violation:
    """One invariant violation and the evidence for it."""

    invariant: Invariant
    detail: str

    def __str__(self) -> str:
        """Name the invariant first, then the evidence."""
        return f"liveness invariant {self.invariant.value} violated: {self.detail}"


@dataclass(frozen=True, slots=True)
class Budget:
    """What bounds a run's work: the scenario's own limits.

    ``retries`` is core's per-request retry limit (``Limits.max_retries``); each unit of
    work may be asked again that many times.
    """

    retries: int = 2


@dataclass(frozen=True, slots=True)
class _Issued:
    request_id: str
    kind: str
    key: str
    owner: str
    label: str


@dataclass(frozen=True, slots=True)
class _Observed:
    request_id: str
    fingerprint: str
    conclusive: bool


type _Entry = _Issued | _Observed

# Request fields that identify one issuance, not what it asks for.
_IDENTITY = {"request_id", "depends_on", "decision_id", "admission_id", "decision_dependencies"}
# Kinds that poll a resource: asking again is how a wait makes progress.
_POLLS = frozenset({"observe_owned_job", "inspect_owned_job", "inspect_request"})
_TURNS = frozenset({"dispatch_turn", "resume_session_turn"})


_SHOWN_LINES = 24
_SHOWN_REPEATS = 8


def _dump(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _label(request: Request) -> str:
    """The request's kind and the identity of what it targets, for a failure message."""
    data = request.model_dump(mode="json")
    parts = [str(data["kind"])]
    parts.extend(
        f"{name}={_dump(data[name])[:60]}"
        for name in ("resource_id", "target", "session_id", "attempt", "operation")
        if data.get(name) is not None
    )
    owner = data["scope"]["owner"]
    parts.append(f"scope={owner.get('kind')}:{owner.get('root', '')}@{data['scope']['generation']}")
    return " ".join(parts)


def _key(request: Request) -> str:
    """The request's kind and target: everything it asks for except who asked and when."""
    data = request.model_dump(mode="json", exclude=_IDENTITY | {"deadline_at"})
    return _dump(data)


def _fingerprint(
    observation: Observation, evidence: Iterable[EvidenceRef], failure: MeasurementFailure | None
) -> str:
    """What an observation says, without the request, sequence or time it carries."""
    return _dump(
        {
            "scope": observation.scope.model_dump(mode="json"),
            "status": observation.status.value,
            "accepted": observation.accepted,
            "terminal": observation.terminal,
            "resource": None
            if observation.resource_id is None
            else observation.resource_id.model_dump(mode="json"),
            "revision": None
            if observation.revision is None
            else observation.revision.model_dump(mode="json"),
            "diagnostic": observation.diagnostic,
            "evidence": sorted(
                _dump([item.evidence_id.model_dump(mode="json"), item.status.value])
                for item in evidence
            ),
            "failure": None if failure is None else failure.value,
        }
    )


@dataclass
class Journal:
    """The requests a run issued and the observations that came back, in order.

    A harness records every request it dispatches with :meth:`issue` and every
    observation it delivers with :meth:`observe`, including the observations of a
    resource that an executor attaches to another request's answer.
    """

    entries: list[_Entry] = field(default_factory=list)

    def issue(self, request: Request) -> None:
        """Record that ``request`` was dispatched."""
        if request.request_id is None:
            message = "a dispatched request carries its identity"
            raise AssertionError(message)
        owner = request.scope.owner
        self.entries.append(
            _Issued(
                request_id=request.request_id.root,
                kind=request.kind,
                key=_key(request),
                owner=_dump(owner.model_dump(mode="json")),
                label=_label(request),
            )
        )

    def observe(
        self,
        observation: Observation,
        evidence: Iterable[EvidenceRef] = (),
        failure: MeasurementFailure | None = None,
    ) -> None:
        """Record one delivered observation, attributed to the request it names."""
        self.entries.append(
            _Observed(
                request_id=observation.request_id.root,
                fingerprint=_fingerprint(observation, evidence, failure),
                conclusive=observation.terminal,
            )
        )

    def observe_event(self, event: RequestObserved) -> None:
        """Record a request's own observation event."""
        self.observe(event.observation, event.evidence, event.measurement_failure)

    @classmethod
    def from_log(cls, log: Iterable[Request | RequestObserved]) -> Journal:
        """Build a journal from a driver's interleaved log of requests and observations."""
        journal = cls()
        for item in log:
            if isinstance(item, RequestObserved):
                journal.observe_event(item)
            else:
                journal.issue(item)
        return journal

    @classmethod
    def from_ledger(cls, core: CoreState) -> Journal:
        """Build a journal from core's intent ledger, in ledger order.

        The ledger keeps each request and its latest observation but not their interleaving,
        so a request is followed by its own observation only.
        """
        journal = cls()
        for intent in sorted(core.intents.intents, key=lambda row: row.sequence or 0):
            journal.issue(intent.request)
            if intent.observation is not None:
                journal.observe(intent.observation, ())
        return journal


def request_bound(journal: Journal, budget: Budget) -> int:
    """The most requests the work the run did can justify.

    Each attempt needs a workspace, a retained revision and an adoption or discard; each
    turn a session and a close; each measurement a submission, an evidence collection and a
    cancellation. Every unit may be asked again ``retries`` times. Polls are not counted: a
    wait is bounded by its owner, not by a count.
    """
    issued = [entry for entry in journal.entries if isinstance(entry, _Issued)]
    owners = {entry.owner for entry in issued if '"kind": "attempt"' in entry.owner}
    keys = Counter(entry.kind for entry in {entry.key: entry for entry in issued}.values())
    turns = sum(count for kind, count in keys.items() if kind in _TURNS)
    measurements = keys["submit_measurement"]
    again = 1 + budget.retries
    run_level = 12
    return run_level + again * (10 * len(owners) + 4 * turns + 6 * measurements)


def _requests(journal: Journal) -> list[_Issued]:
    return [entry for entry in journal.entries if isinstance(entry, _Issued)]


def _render(issued: Sequence[_Issued], marked: set[int]) -> str:
    lines = [f"  #{i}{' <-' if i in marked else '  '} {row.label}" for i, row in enumerate(issued)]
    if len(lines) > _SHOWN_LINES:
        lines = [*lines[:6], f"  ... {len(lines) - 18} more ...", *lines[-12:]]
    return "\n".join(lines)


def _bound_violation(journal: Journal, budget: Budget) -> list[Violation]:
    issued = [row for row in _requests(journal) if row.kind not in _POLLS]
    bound = request_bound(journal, budget)
    if len(issued) <= bound:
        return []
    counts = Counter(row.kind for row in issued).most_common(3)
    return [
        Violation(
            Invariant.UNBOUNDED_REQUESTS,
            f"{len(issued)} non-poll requests exceed the {bound} the run's attempts, turns "
            f"and measurements justify; most issued: {counts}. Requests:\n"
            + _render(_requests(journal), set()),
        )
    ]


@dataclass
class _Last:
    """The latest issuance of one request key and what has happened since."""

    index: int
    request_id: str
    answered: bool = False
    fresh: bool = False


def spin_violations(journal: Journal) -> list[Violation]:
    """A request repeated after a conclusive answer with no new observation since.

    An observation is new when no earlier observation had its content, and it counts for
    a request only when it is not that request's own answer.
    """
    seen: set[str] = set()
    last: dict[str, _Last] = {}
    issued: list[_Issued] = []
    repeats: dict[str, list[int]] = {}
    for entry in journal.entries:
        if isinstance(entry, _Issued):
            previous = last.get(entry.key)
            if (
                previous is not None
                and previous.answered
                and not previous.fresh
                and entry.kind not in _POLLS
            ):
                repeats.setdefault(entry.key, [previous.index]).append(len(issued))
            issued.append(entry)
            last[entry.key] = _Last(index=len(issued) - 1, request_id=entry.request_id)
            continue
        novel = entry.fingerprint not in seen
        seen.add(entry.fingerprint)
        for row in last.values():
            if row.request_id == entry.request_id:
                row.answered = row.answered or entry.conclusive
            elif novel:
                row.fresh = True
    return [
        Violation(
            Invariant.SPIN,
            f"{len(indexes) - 1} repeats of {issued[indexes[0]].label}, each issued after the "
            "previous one was answered and with no new observation in between "
            f"(requests {indexes[:_SHOWN_REPEATS]}{'...' if len(indexes) > _SHOWN_REPEATS else ''}):\n"
            + _render(issued, set(indexes)),
        )
        for indexes in repeats.values()
    ]


class End(StrEnum):
    """How the scenario expects the run to end."""

    # The run reaches a terminal status and leaves nothing open.
    TERMINAL = "terminal"
    # An operator stop interrupted the run: it stays resumable, so its status is not
    # terminal and requests in flight at the stop stay open for the resume to recover.
    STOPPED = "stopped"
    # The driver halts the run on purpose before its end (it has no executor for an agent
    # turn), so only the request bound and the repeat rule apply.
    CUT_SHORT = "cut_short"


def final_state(core: CoreState, end: End = End.TERMINAL) -> list[Violation]:
    """Quiescence: no wait without a producer, and for a finished run a terminal status.

    A finished run also has no open intent.
    """
    found: list[Violation] = []
    if end is End.TERMINAL:
        found.extend(_unfinished(core))
    orphans = orphan_waits(core)
    if orphans:
        listing = "; ".join(f"{wait.waiter.value} {wait.subject}" for wait in orphans[:6])
        found.append(
            Violation(Invariant.ORPHAN_WAIT, f"{len(orphans)} without an owner: {listing}")
        )
    return found


def _unfinished(core: CoreState) -> list[Violation]:
    found: list[Violation] = []
    if core.run.status is not RunStatus.TERMINAL:
        found.append(
            Violation(
                Invariant.NOT_TERMINAL, f"quiescent with run status {core.run.status.value!r}"
            )
        )
    open_intents: list[Intent] = [row for row in core.intents.intents if phase_waits(row.phase)]
    if open_intents:
        listing = "; ".join(
            f"{row.request.kind} {row.request_id.root} is {row.phase.value}"
            for row in open_intents[:6]
        )
        found.append(Violation(Invariant.OPEN_INTENT, f"{len(open_intents)} open: {listing}"))
    return found


def liveness(
    journal: Journal, core: CoreState, budget: Budget | None = None, end: End = End.TERMINAL
) -> list[Violation]:
    """Every liveness violation of a finished run that was expected to end as ``end``."""
    return [
        *_bound_violation(journal, budget or Budget()),
        *spin_violations(journal),
        *([] if end is End.CUT_SHORT else final_state(core, end)),
    ]


def assert_live(
    journal: Journal, core: CoreState, budget: Budget | None = None, end: End = End.TERMINAL
) -> None:
    """Fail with the violated invariants and the offending request sequence."""
    violations = liveness(journal, core, budget, end)
    if violations:
        raise LivenessViolationError("\n".join(str(item) for item in violations))
