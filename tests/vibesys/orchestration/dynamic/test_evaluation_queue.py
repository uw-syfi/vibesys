"""The evaluation queue binds a free slot to the candidate its policy picks, over any order.

The queue is pure: workstreams submit candidates, measurements end and free their slots, the
orchestrator ranks. Whatever the order of those events, the properties hold: the queue never holds
more measurements than it has slots and never leaves a slot free while a candidate waits; each
bind takes the newest waiting candidate (or the orchestrator's ranked ones first) unless an older
one is due; a candidate is dropped only when a newer one of its line holds its code; and every
submission is in exactly one place (waiting, measuring or measured, or dropped) with at most one verdict. No submission waits
forever behind newer ones: each is overtaken at most once per slot, and a stop drops every one
still waiting, so each is bound, superseded or dropped at the stop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vibesys.orchestration.dynamic.search.api import (
    Bound,
    Dropped,
    DropReason,
    EvaluationQueue,
    SlotVerdict,
    Submission,
    WorkKind,
)
from vs_core.api import RevisionRef
from vs_sim.api.testing import examples

BASE = RevisionRef.of_git_commit("input")
LINES = ("a", "b", "c")


@dataclass(frozen=True)
class SubmitCandidate:
    """A workstream of ``line`` submits a candidate built on an earlier one (or on the input)."""

    line: str
    kind: WorkKind
    parent: int | None
    """Which earlier submission's candidate it is built on, modulo how many; None: the input."""
    at_parent: bool = False
    """It made no code of its own: the revision measured is its parent (a profile's always is)."""


@dataclass(frozen=True)
class End:
    """The measurement holding a slot (which one, modulo how many) ends."""

    which: int


@dataclass(frozen=True)
class Rank:
    """The orchestrator orders the lines to measure first."""

    order: tuple[str, ...]


type Event = SubmitCandidate | End | Rank

EVENTS = st.one_of(
    st.builds(
        SubmitCandidate,
        st.sampled_from(LINES),
        st.sampled_from([WorkKind.IMPLEMENT, WorkKind.IMPLEMENT, WorkKind.PROFILE]),
        st.none() | st.integers(0, 20),
        st.booleans(),
    ),
    st.builds(End, st.integers(0, 10)),
    st.builds(Rank, st.permutations(LINES).map(tuple) | st.just(())),
)


@dataclass
class Played:
    """What a run of the queue decided, for the properties to check."""

    queue: EvaluationQueue
    submitted: dict[str, Submission] = field(default_factory=dict)
    verdicts: dict[str, list[SlotVerdict]] = field(default_factory=dict)
    measured: set[str] = field(default_factory=set)
    overtaken: dict[str, int] = field(default_factory=dict)
    """For each submission, how many submissions made after it were bound while it waited."""


def _ancestors(played: Played, revision: RevisionRef) -> set[RevisionRef]:
    """``revision`` and every submitted candidate's parent on its line, back to the input."""
    parents = {
        item.candidate: item.parent
        for item in played.submitted.values()
        if item.kind is WorkKind.IMPLEMENT and item.candidate != item.parent
    }
    found: set[RevisionRef] = set()
    current: RevisionRef | None = revision
    while current is not None and current not in found:
        found.add(current)
        current = parents.get(current)
    return found


def _submission(played: Played, event: SubmitCandidate, at: int) -> Submission:
    earlier = list(played.submitted.values())
    parent = BASE
    if event.parent is not None and earlier:
        parent = earlier[event.parent % len(earlier)].candidate
    made = parent if event.at_parent or event.kind is WorkKind.PROFILE else None
    return Submission(
        key=f"{at:06d}.{event.line}",
        work_id=event.line,
        kind=event.kind,
        candidate=made or RevisionRef.of_git_commit(f"c{at}"),
        parent=parent,
        at=float(at),
    )


def _bind_checked(played: Played) -> None:
    """Bind, and check the first free slot went to the first pick, every free slot filled."""
    queue = played.queue
    waiting = {item.submission.key: item.submission.at for item in queue.pending}
    expected = [item.submission.key for item in queue.in_order()][:1]
    free = queue.free
    queue.bind()
    decided = queue.verdicts()
    bound = [key for key, verdict in decided if isinstance(verdict, Bound)]
    assert bound[:1] == (expected if free else [])
    assert len(bound) == min(free, len(waiting))
    assert set(bound) <= set(waiting)
    assert len(queue.holding) <= queue.slots
    assert not queue.pending or queue.free == 0
    for position, key in enumerate(bound):
        for other, at in waiting.items():
            if other not in bound[: position + 1] and at < waiting[key]:
                played.overtaken[other] = played.overtaken.get(other, 0) + 1
    for key, verdict in decided:
        played.verdicts.setdefault(key, []).append(verdict)


def _submit_checked(played: Played, event: SubmitCandidate, at: int) -> None:
    queue = played.queue
    submission = _submission(played, event, at)
    waiting = {item.submission.key: item.submission for item in queue.pending}
    played.submitted[submission.key] = submission
    queue.submit(submission)
    for key, verdict in queue.verdicts():
        played.verdicts.setdefault(key, []).append(verdict)
        # only a waiting implement candidate whose code the new one holds is dropped
        older = waiting[key]
        assert isinstance(verdict, Dropped)
        assert verdict.reason is DropReason.SUPERSEDED
        assert verdict.by == submission.work_id
        assert submission.kind is WorkKind.IMPLEMENT
        assert older.kind is WorkKind.IMPLEMENT
        # a candidate measured at its parent made no code, so it holds no other's work
        assert submission.candidate != submission.parent
        assert older.work_id == submission.work_id or older.candidate in _ancestors(
            played, submission.parent
        )


def play(events: list[Event], *, slots: int, ranked: bool) -> Played:
    """Run ``events`` through a queue of ``slots``, checking each step's properties."""
    played = Played(EvaluationQueue(slots=slots, policy="ranked" if ranked else "newest"))
    for at, event in enumerate(events, 1):
        match event:
            case SubmitCandidate():
                _submit_checked(played, event, at)
            case End(which=which) if played.queue.holding:
                key = played.queue.holding[which % len(played.queue.holding)]
                played.queue.release(key)
                played.measured.add(key)
            case End():
                pass
            case Rank(order=order):
                played.queue.rank(order)
        _bind_checked(played)
    return played


@settings(max_examples=examples(pr=60, nightly=1000))
@given(events=st.lists(EVENTS, max_size=40), slots=st.integers(1, 3), ranked=st.booleans())
@example(
    events=[
        SubmitCandidate("a", WorkKind.IMPLEMENT, None),
        SubmitCandidate("b", WorkKind.IMPLEMENT, None),
        SubmitCandidate(
            "c", WorkKind.IMPLEMENT, 1, at_parent=True
        ),  # measured at b's waiting candidate
    ],
    slots=1,
    ranked=False,
)
def test_any_order_of_submissions_and_ends_keeps_every_queue_property(
    events: list[Event], slots: int, *, ranked: bool
) -> None:
    played = play(events, slots=slots, ranked=ranked)
    queue = played.queue

    # every submission is in exactly one place, with at most one verdict
    waiting = {item.submission.key for item in queue.pending}
    holding = set(queue.holding)
    dropped = {item.submission.key for item, _ in queue.dropped}
    bound = {key for key, made in played.verdicts.items() if isinstance(made[0], Bound)}
    assert all(len(made) == 1 for made in played.verdicts.values())
    assert waiting | bound | dropped == set(played.submitted)
    assert not waiting & (bound | dropped)
    assert holding <= bound
    assert bound - holding == played.measured & bound

    # no waiting implement candidate holds the code of another waiting one of its line (one
    # measured at its parent made no code of its own)
    pending = [
        item.submission
        for item in queue.pending
        if item.submission.kind is WorkKind.IMPLEMENT
        and item.submission.candidate != item.submission.parent
    ]
    for newer in pending:
        for older in pending:
            if older.key != newer.key and older.at < newer.at:
                assert older.work_id != newer.work_id
                assert older.candidate not in _ancestors(played, newer.parent)


@settings(max_examples=examples(pr=40, nightly=500))
@given(
    lines=st.lists(st.sampled_from(LINES), min_size=1, max_size=8),
    order=st.permutations(LINES).map(tuple),
    ranked=st.booleans(),
)
def test_a_free_slot_measures_the_newest_or_the_first_ranked_candidate(
    lines: list[str], order: tuple[str, ...], *, ranked: bool
) -> None:
    # one slot, held by a first measurement, while profiles of the lines wait (never superseded)
    queue = EvaluationQueue(slots=1, policy="ranked" if ranked else "newest")
    queue.hold("input")
    queue.rank(order)
    for at, line in enumerate(lines, 1):
        queue.submit(
            Submission(
                key=f"{at:06d}.{line}",
                work_id=line,
                kind=WorkKind.PROFILE,
                candidate=RevisionRef.of_git_commit(f"c{at}"),
                parent=BASE,
                at=float(at),
            )
        )
    queue.bind()
    assert queue.verdicts() == []  # the slot is held: nothing is bound while it is

    queue.release("input")
    queue.bind()
    ((key, verdict),) = queue.verdicts()
    assert isinstance(verdict, Bound)
    newest = f"{len(lines):06d}.{lines[-1]}"
    if not ranked:
        assert key == newest
    else:
        first = min(lines, key=order.index)
        last_of_first = max(at for at, line in enumerate(lines, 1) if line == first)
        assert key == f"{last_of_first:06d}.{first}"


ARRIVALS = st.one_of(
    st.builds(SubmitCandidate, st.sampled_from(LINES), st.just(WorkKind.PROFILE), st.none()),
    st.builds(End, st.integers(0, 10)),
)
"""Submissions that are never superseded, and measurements that end: a run that keeps submitting."""


@settings(max_examples=examples(pr=60, nightly=1000))
@given(
    events=st.lists(ARRIVALS, max_size=60) | st.lists(EVENTS, max_size=60),
    slots=st.integers(1, 3),
    ranked=st.booleans(),
)
@example(
    events=[SubmitCandidate("a", WorkKind.PROFILE, None)]
    + [SubmitCandidate("b", WorkKind.PROFILE, None)] * 2
    + [End(0), SubmitCandidate("c", WorkKind.PROFILE, None)] * 3,
    slots=1,
    ranked=False,
)
def test_no_waiting_submission_is_overtaken_more_than_once_per_slot(
    events: list[Event], slots: int, *, ranked: bool
) -> None:
    """The oldest submissions hold the backlog's places, so none may wait forever behind newer
    ones: each is bound after at most ``slots`` submissions made after it."""
    played = play(events, slots=slots, ranked=ranked)

    assert all(count <= slots for count in played.overtaken.values()), played.overtaken


@settings(max_examples=examples(pr=60, nightly=1000))
@given(events=st.lists(EVENTS, max_size=40), slots=st.integers(1, 3), ranked=st.booleans())
def test_every_submission_is_bound_superseded_or_dropped_at_the_stop(
    events: list[Event], slots: int, *, ranked: bool
) -> None:
    played = play(events, slots=slots, ranked=ranked)
    waiting = {item.submission.key for item in played.queue.pending}

    played.queue.stop()
    for key, verdict in played.queue.verdicts():
        played.verdicts.setdefault(key, []).append(verdict)
        assert key in waiting
        assert isinstance(verdict, Dropped)
        assert verdict.reason is DropReason.STOPPED

    assert not played.queue.pending
    assert set(played.verdicts) == set(played.submitted)
    assert all(len(made) == 1 for made in played.verdicts.values())
