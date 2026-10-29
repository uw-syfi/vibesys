"""The run's trusted evaluations: candidates wait in a queue and are bound to a slot when it starts.

A workstream whose candidate is ready for its trusted measurement does not reserve a measurement
up front. It submits the candidate (`Submission`) and waits; the root keeps every submission in
an `EvaluationQueue`, and the run holds at most ``slots`` trusted measurements at once (the
input's own measurement holds one from the start, so it always goes first). Only when a slot is
free does the queue decide which submission it measures, by the run's pick policy:

- ``newest`` (the default): the submission made last, since an older one belongs to an older
  implementation and matters less;
- ``ranked``: the order the orchestrator last gave (`rank`), and the submissions it did not rank
  after those, newest first.

Either policy alone could starve a submission: under ``newest``, each new submission is bound
before the older ones, so a run that keeps submitting never binds its oldest, and the oldest are
the ones that hold the backlog's places (`RunRecord.parked`). So a submission is overtaken (a
submission received after it is bound while it waits) at most ``slots`` times: after that it is
due, and due submissions are bound before any other, oldest first. Every waiting submission is
therefore bound after at most ``slots`` newer ones, or superseded, or dropped at a stop.

A submission is dropped, with the reason recorded, when a newer submission supersedes it (the
same workstream line, or a candidate built on it: its code is in the newer one), or when the run
stops before a slot measures it. Each decision
is a `SlotVerdict` for the workstream that submitted (`verdicts`), which the root hands over. How
many submissions may wait is not the queue's to bound: a waiting workstream past the run's
backlog keeps its agent slot instead (`RunRecord.working`), so the backlog applies backpressure
on new work rather than dropping finished work.

Everything here is a plain function of what was submitted, released and ranked: no clock (the
times are what the submitters read), no I/O. When the backends report slots that start on their
own (a pilot job that starts in a cluster queue), `bind` is what answers each start.
"""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import Field

from vibesys.orchestration.dynamic.search._values import DropReason, WorkKind
from vs_core.api.search import RevisionRef, Value

type PickPolicy = Literal["newest", "ranked"]
"""How a free evaluation slot picks among the submissions waiting for one."""


class Submission(Value):
    """A candidate (or a revision to profile) ready for its trusted measurement."""

    key: str
    """The workstream that submitted it, and that waits for its verdict."""
    work_id: str
    """The hypothesis, profile or plan step the workstream works on: its line."""
    kind: WorkKind
    candidate: RevisionRef
    """The revision to measure."""
    parent: RevisionRef
    """The revision the candidate was built on (a profile's is its target)."""
    at: float = Field(ge=0)
    """When it was submitted, on the run clock."""


class Queued(Value):
    """A submission in the queue, numbered in the order the queue received it."""

    submission: Submission
    order: int = Field(gt=0)


class Bound(Value):
    """The submission holds a slot: measure it now."""

    kind: Literal["bound"] = "bound"


class Dropped(Value):
    """The submission will never be measured, and why."""

    kind: Literal["dropped"] = "dropped"
    reason: DropReason
    by: str | None = None
    """For a superseded submission, the work id of the submission that superseded it."""


type SlotVerdict = Annotated[Bound | Dropped, Field(discriminator="kind")]
"""What the queue decided for a submission: measured now, or never."""


class QueueSnapshot(Value):
    """The queue as it is published and shown to the orchestrator."""

    policy: PickPolicy
    slots: int
    holding: tuple[str, ...]
    """The keys of the measurements that hold a slot, in the order they were bound."""
    pending: tuple[Queued, ...]
    """The submissions waiting for a slot, in the order the policy would pick them."""
    ranking: tuple[str, ...]
    dropped: tuple[tuple[Queued, Dropped], ...]
    last_bound: Queued | None


class QueueState(Value):
    """Everything the queue knows, so a restart after a code change resumes it as it was.

    The slots and the pick policy are the run's config; the verdicts not yet handed over are
    not kept: the root hands every decided verdict again after a restart
    (`RunRecord.decided_verdicts`).
    """

    pending: tuple[Queued, ...]
    holding: tuple[str, ...]
    ranking: tuple[str, ...]
    dropped: tuple[tuple[Queued, Dropped], ...]
    last_bound: Queued | None
    received: int
    built_on: tuple[tuple[RevisionRef, RevisionRef], ...]
    overtaken: tuple[tuple[str, int], ...]


@dataclass
class EvaluationQueue:
    """The submissions waiting for a trusted measurement, and the slots that measure them."""

    slots: int
    """Trusted measurements the run holds at once, the input's included."""
    policy: PickPolicy = "newest"
    pending: list[Queued] = field(default_factory=list)
    holding: list[str] = field(default_factory=list)
    ranking: tuple[str, ...] = ()
    """The work ids the orchestrator ranked, first to measure first (``ranked`` only)."""
    dropped: list[tuple[Queued, Dropped]] = field(default_factory=list)
    last_bound: Queued | None = None
    received: int = 0
    built_on: dict[RevisionRef, RevisionRef] = field(default_factory=dict)
    """Each submitted candidate's parent: the lines a newer submission continues."""
    overtaken: dict[str, int] = field(default_factory=dict)
    """For each waiting submission, by key, how many submissions received after it were bound."""
    _verdicts: list[tuple[str, SlotVerdict]] = field(default_factory=list)

    @property
    def free(self) -> int:
        """Slots no measurement holds."""
        return max(self.slots - len(self.holding), 0)

    def hold(self, key: str) -> None:
        """A measurement that takes a slot without waiting in the queue: the input's, first."""
        if key not in self.holding:
            self.holding.append(key)

    def submit(self, submission: Submission) -> None:
        """Queue ``submission``, and drop what it supersedes."""
        self.received += 1
        entry = Queued(submission=submission, order=self.received)
        if submission.kind is WorkKind.IMPLEMENT and submission.candidate != submission.parent:
            # a candidate measured at its parent made no code: it continues no line, holds no
            # other submission's work, and must not rewrite where its parent was built
            self.built_on[submission.candidate] = submission.parent
            for older in [item for item in self.pending if self._supersedes(entry, item)]:
                self._drop(older, Dropped(reason=DropReason.SUPERSEDED, by=submission.work_id))
        self.pending.append(entry)

    def rank(self, order: tuple[str, ...]) -> None:
        """Take the orchestrator's order of work ids to measure first."""
        self.ranking = order

    def bind(self) -> None:
        """Bind a submission to each free slot, the policy's first pick first."""
        while self.free and self.pending:
            chosen = self.in_order()[0]
            self.pending.remove(chosen)
            self.overtaken.pop(chosen.submission.key, None)
            for older in self.pending:
                if older.order < chosen.order:
                    key = older.submission.key
                    self.overtaken[key] = self.overtaken.get(key, 0) + 1
            self.holding.append(chosen.submission.key)
            self.last_bound = chosen
            self._verdicts.append((chosen.submission.key, Bound()))

    def stop(self) -> None:
        """The run is stopping: drop every waiting submission, none is bound any more."""
        for entry in list(self.pending):
            self._drop(entry, Dropped(reason=DropReason.STOPPED))

    def release(self, key: str) -> None:
        """The measurement of ``key`` is over (or it never ran): its slot is free."""
        if key in self.holding:
            self.holding.remove(key)

    def forget(self, key: str) -> None:
        """The workstream of ``key`` ended: free its slot and take back what it submitted."""
        self.release(key)
        self.pending = [item for item in self.pending if item.submission.key != key]
        self.overtaken.pop(key, None)
        self._verdicts = [item for item in self._verdicts if item[0] != key]

    def verdicts(self) -> list[tuple[str, SlotVerdict]]:
        """The verdicts decided since the last call, in the order they were decided."""
        decided, self._verdicts = self._verdicts, []
        return decided

    def in_order(self) -> list[Queued]:
        """The waiting submissions in the order they are bound: the due ones, then the policy's."""
        due = sorted(
            (
                item
                for item in self.pending
                if self.overtaken.get(item.submission.key, 0) >= self.slots
            ),
            key=lambda item: item.order,
        )
        rest = [item for item in self.pending if item not in due]
        newest = sorted(rest, key=lambda item: -item.order)
        if self.policy == "newest":
            return [*due, *newest]
        rank = {work_id: position for position, work_id in enumerate(self.ranking)}
        ranked = sorted(newest, key=lambda item: rank.get(item.submission.work_id, len(rank)))
        return [*due, *ranked]

    def snapshot(self) -> QueueSnapshot:
        """The queue as it is published."""
        return QueueSnapshot(
            policy=self.policy,
            slots=self.slots,
            holding=tuple(self.holding),
            pending=tuple(self.in_order()),
            ranking=self.ranking,
            dropped=tuple(self.dropped),
            last_bound=self.last_bound,
        )

    def state(self) -> QueueState:
        """What the queue knows, to resume it from (`resumed`)."""
        return QueueState(
            pending=tuple(self.pending),
            holding=tuple(self.holding),
            ranking=self.ranking,
            dropped=tuple(self.dropped),
            last_bound=self.last_bound,
            received=self.received,
            built_on=tuple(self.built_on.items()),
            overtaken=tuple(self.overtaken.items()),
        )

    @classmethod
    def resumed(cls, slots: int, policy: PickPolicy, state: QueueState) -> "EvaluationQueue":
        """The queue ``state`` describes, with the run's ``slots`` and ``policy``."""
        return cls(
            slots=slots,
            policy=policy,
            pending=list(state.pending),
            holding=list(state.holding),
            ranking=state.ranking,
            dropped=list(state.dropped),
            last_bound=state.last_bound,
            received=state.received,
            built_on=dict(state.built_on),
            overtaken=dict(state.overtaken),
        )

    def _supersedes(self, newer: Queued, older: Queued) -> bool:
        """Whether ``newer`` holds the work of ``older``: the same line, or built on it."""
        if older.submission.kind is not WorkKind.IMPLEMENT:
            return False
        if older.submission.work_id == newer.submission.work_id:
            return True
        return older.submission.candidate in self._ancestors(newer.submission.parent)

    def _ancestors(self, revision: RevisionRef) -> set[RevisionRef]:
        """``revision`` and every revision a submitted candidate on its line was built on."""
        found: set[RevisionRef] = set()
        current: RevisionRef | None = revision
        while current is not None and current not in found:
            found.add(current)
            current = self.built_on.get(current)
        return found

    def _drop(self, entry: Queued, dropped: Dropped) -> None:
        self.pending.remove(entry)
        self.overtaken.pop(entry.submission.key, None)
        self.dropped.append((entry, dropped))
        self._verdicts.append((entry.submission.key, dropped))
