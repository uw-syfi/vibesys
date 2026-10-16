"""The plain values the evolve search keeps and passes around, with no behavior of their own.

Every value is a strict, frozen `Value` with closed fields. Reasons are enums with the typed
facts that go with them; the only free text is text an agent or an evaluator wrote (a reviewer's
feedback, what a failed check printed), carried as data for a later prompt.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from pydantic import Field

from vibesys.candidates.api import STOPPED as STOPPED_TEXT
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading
from vibesys.orchestration.evolve.population import Metrics
from vibesys.orchestration.evolve.search._config import EvolveConfig
from vibesys.orchestration.evolve.search._prompts import MutatePrompt
from vs_core.api.search import (
    AttemptBudget,
    AttemptId,
    CheckpointDecline,
    FailureReason,
    InvocationRef,
    ItemId,
    RevisionRef,
    Settlement,
    TurnFailureKind,
    Value,
    WorkspaceMode,
    WorkspacePlan,
    attempt_id,
    item_id,
)


class Stage(StrEnum):
    """Where the search is: still looking for its first passing individual, or evolving."""

    BOOTSTRAP = "bootstrap"
    GENERATION = "generation"


# -- why a child ended ----------------------------------------------------------------------------


class FailureKind(StrEnum):
    """Why a child ended without being a trustworthy individual. Closed."""

    STOPPED = "stopped"
    """The operator stopped the run."""
    NO_REPLY = "no-reply"
    """An agent's turn ended without a valid reply; ``role`` and ``ended``/``turn_failure`` say how."""
    CHECKPOINT_DECLINED = "checkpoint-declined"
    """Core could not retain the workspace the mutator left; ``declined`` says why."""
    UNMEASURED = "unmeasured"
    """The machinery could not measure the candidate, which says nothing about it."""
    EVIDENCE_REFUSED = "evidence-refused"
    EVIDENCE_UNDECODABLE = "evidence-undecodable"
    PATCH_EXPORT_FAILED = "patch-export-failed"
    NOT_OPENED = "not-opened"
    """Core admitted no workspace for the child; ``ended`` says why."""
    PARENT_UNVERIFIED = "parent-unverified"
    """The parent revision no longer reproduces its retained content."""


class Failure(Value):
    """A typed account of why a child ended, with the facts its kind has."""

    kind: FailureKind
    role: str | None = None
    ended: FailureReason | None = None
    turn_failure: TurnFailureKind | None = None
    declined: CheckpointDecline | None = None
    status: str | None = None
    detail: str = ""
    """What an evaluator or an exporter printed, kept as captured data."""

    @property
    def feedback(self) -> str:
        """The note a failed individual keeps for the mutators that come after it."""
        how = self.turn_failure or self.ended
        facts = {
            "role": self.role,
            "how": "unknown" if how is None else how.value,
            "declined": self.declined and self.declined.value,
            "status": self.status or (self.ended and self.ended.value),
            "detail": self.detail,
        }
        return _FEEDBACK[self.kind].format_map(facts)


_FEEDBACK: dict[FailureKind, str] = {
    FailureKind.STOPPED: STOPPED_TEXT,
    FailureKind.NO_REPLY: "framework: the {role} returned no valid response ({how})",
    FailureKind.CHECKPOINT_DECLINED: "framework: mutator checkpoint declined ({declined})",
    FailureKind.UNMEASURED: "the candidate could not be measured ({status})",
    FailureKind.EVIDENCE_REFUSED: "framework: evidence readings refused",
    FailureKind.EVIDENCE_UNDECODABLE: "framework: evidence could not be decoded ({status})",
    FailureKind.PATCH_EXPORT_FAILED: "framework: patch export {status}: {detail}",
    FailureKind.NOT_OPENED: "framework: no workspace was opened for the attempt",
    FailureKind.PARENT_UNVERIFIED: "framework: the parent revision could not be verified",
}


STOPPED = Failure(kind=FailureKind.STOPPED)
"""How an operator stop ends a child."""


# -- what a child is told, what it establishes ---------------------------------------------------------


class SlotProposal(Value):
    """The selector's choice for one slot of a generation, by individual id."""

    parent_id: int = Field(gt=0)
    inspiration_ids: tuple[int, ...] = ()
    policy_parent_id: str | None = None
    target_island: int | None = None


class ChildStart(Value):
    """One scheduled child: the brief of its mutation and the revision it builds on, frozen."""

    key: str = Field(min_length=1)
    """Stable subject: ``bootstrap.<k>`` for the k-th cold start, ``g<generation>.c<slot>``."""
    slot: int = Field(gt=0)
    generation: int = Field(ge=0)
    """The search generation of the child; 0 for a cold start."""
    round: int = Field(default=1, gt=0)
    """The round that runs the child: one cold start or one generation, counted from 1."""
    proposal: SlotProposal | None = None
    base: RevisionRef
    prompt: MutatePrompt
    config: EvolveConfig

    @property
    def cold_start(self) -> bool:
        """Whether the child seeds the population rather than mutating a parent."""
        return self.generation == 0

    @property
    def attempt(self) -> AttemptId:
        """The identity of the attempt this child opens."""
        return attempt_id(self.key, 1)

    @property
    def item(self) -> ItemId:
        """The scientific item the attempt works on."""
        return item_id(self.key, 1)

    @property
    def workspace(self) -> WorkspacePlan:
        """A child works in an isolated copy of its base, or in the run's root where there is none."""
        mode = (
            WorkspaceMode.EXCLUSIVE_ROOT
            if self.config.exclusive_root
            else WorkspaceMode.ISOLATED_CHILD
        )
        return WorkspacePlan(mode=mode, base=self.base)

    @property
    def budget(self) -> AttemptBudget:
        """One admitted attempt with one paid turn: a mutation is never asked again."""
        return AttemptBudget(admission_charge=1, paid_invocation_limit=1)


class Verdict(Value):
    """The judge's decision on a candidate, with the turn it came from."""

    passed: bool
    invocation: InvocationRef


class ProfileReading(Value):
    """What the advisory profiler reported, the part that can become fitness."""

    perf_metric: float | None = Field(default=None, allow_inf_nan=False)
    perf_unit: str | None = None
    metrics: Metrics = ()


class Closing(Value):
    """What the close of the workspace asserts: the finished attempt the settlement judges.

    It reads as the `FinishedAttempt` that candidate policy settles.
    """

    revision: RevisionRef | None = None
    """The candidate the mutator left and core retained."""
    summary: str = ""
    verdict: Verdict | None = None
    feedback: str = ""
    """What the next mutators read: the reviewer's, a failed gate's, or a framework note."""
    readings: tuple[AcceptedReading, ...] = ()
    profile: ProfileReading | None = None
    patch: str | None = None
    passed: bool = False
    """The candidate passed review and every configured gate, and nothing went wrong after."""
    cause: Failure | None = None
    notes: tuple[str, ...] = ()
    """Advisory failures (the profiler's) for an operator to read."""

    @property
    def candidate(self) -> RevisionRef | None:
        """The revision the attempt retained, if any."""
        return self.revision

    @property
    def review_passed(self) -> bool | None:
        """The judge's verdict, or None when no review ran."""
        return None if self.verdict is None else self.verdict.passed

    @property
    def judge_invocation(self) -> InvocationRef | None:
        """The judge turn the verdict came from."""
        return None if self.verdict is None else self.verdict.invocation

    @property
    def outcome(self) -> HypothesisOutcome | None:
        """Evolve has no implementer outcome: a mutation either leaves a candidate or fails."""
        return None

    @property
    def failure(self) -> str | None:
        """The recorded failure as candidate policy reads it: the operator's stop, or its kind."""
        if self.cause is None:
            return None
        return STOPPED_TEXT if self.cause.kind is FailureKind.STOPPED else self.cause.kind.value

    @property
    def unmeasured(self) -> bool:
        """The candidate was never measured (the machinery failed), so it earns no individual."""
        return self.cause is not None and self.cause.kind is FailureKind.UNMEASURED

    def ended_by(self, cause: Failure) -> "Closing":
        """The same closing, ended by ``cause``; a failed individual keeps a framework note."""
        return self.model_copy(
            update={
                "cause": cause,
                "passed": False,
                "feedback": self.feedback or cause.feedback,
            }
        )


@dataclass(frozen=True)
class ChildDone:
    """The child is over; the search records it.

    ``settlement`` is core's settlement of the workspace; None if the workspace never opened or
    core took none, in which case the child earns no individual.
    """

    start: ChildStart
    closing: Closing = field(default_factory=Closing)
    settlement: Settlement | None = None
    refused: Failure | None = None
    """Why the child never got a workspace (or a verified parent); it used no slot."""

    @property
    def passed(self) -> bool:
        """The candidate passed everything and core accepted it as eligible."""
        settlement = self.settlement
        return (
            self.closing.passed
            and settlement is not None
            and settlement.eligible
            and self.closing.cause is None
        )

    @property
    def feedback(self) -> str:
        """The individual's feedback: a candidate core would not accept says so."""
        if self.closing.passed and not self.passed and not self.closing.feedback:
            return "framework: core did not accept the candidate as eligible"
        return self.closing.feedback
