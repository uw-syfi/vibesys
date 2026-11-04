"""Pure validation of a planner portfolio against what the search has recorded.

Every rejection is a typed `PlanViolation` naming the offending plan field, so the planner
correction can quote it from a template. Validation never raises: it splits a portfolio into the
entries that may be scheduled and the violations that explain the rest.
"""

from dataclasses import dataclass, field
from typing import Literal

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.orchestration.dynamic.models import (
    CombinePlan,
    MergePlan,
    PortfolioPlan,
    ProfileOffer,
    ProfilePlan,
    StrategyReason,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.search._main import MergeOptions
from vibesys.orchestration.dynamic.search._parents import ParentOption
from vibesys.orchestration.dynamic.search._values import (
    Alternative,
    CombinePick,
    Expectation,
    HandoffNote,
    HypothesisRecord,
    PlanViolation,
    ViolationCode,
    WorkKind,
    WorkPlan,
)
from vs_core.api.search import Value

_TERMINAL_OUTCOMES = frozenset(
    {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED, HypothesisOutcome.DISPROVEN}
)

_RETIREMENTS: dict[str, Literal[HypothesisStrategy.PARKED, HypothesisStrategy.ABANDONED]] = {
    "parked": HypothesisStrategy.PARKED,
    "abandoned": HypothesisStrategy.ABANDONED,
}
"""The planner's wording of a retirement, as the strategic value the record stores."""


class PlanUpdate(Value):
    """A validated park or abandon decision for a finished hypothesis."""

    hypothesis_id: str
    disposition: Literal[HypothesisStrategy.PARKED, HypothesisStrategy.ABANDONED]
    reason: str
    reason_kind: StrategyReason


class PlanCheck(Value):
    """The schedulable part of a portfolio and the violations that excluded the rest.

    ``reasoning`` and ``handoff`` are the reply's own, kept with what was accepted of it so the
    handoff file of a scheduled plan says what that reply said.
    """

    reasoning: str
    handoff: HandoffNote
    accepted: tuple[WorkPlan, ...] = ()
    updates: tuple[PlanUpdate, ...] = ()
    violations: tuple[PlanViolation, ...] = ()
    ranking: tuple[str, ...] | None = None
    """The order to measure waiting candidates in; None when the reply gave none."""

    @property
    def valid(self) -> bool:
        """Whether the whole portfolio was accepted unchanged."""
        return not self.violations


@dataclass(frozen=True)
class PlanLedger:
    """What validation reads of the search: hypotheses, what is running and which IDs are taken.

    ``merge_ids`` are the IDs merges were scheduled under; ``merging`` says a merge is running or
    accepted and not yet started, so another one would start from a main about to move;
    ``merge_options`` says which candidates a merge may name, by the rule the prompt lists them by.
    """

    hypotheses: tuple[HypothesisRecord, ...]
    running: frozenset[str]
    profile_ids: frozenset[str]
    repairable: frozenset[str] | None = None
    """While the task's plan says what runs, the failed steps a reply may continue, and
    nothing else; None when any workstream may be proposed."""
    ranks: bool = False
    """Whether the run lets the reply order its trusted evaluations."""
    submitted: frozenset[str] = frozenset()
    """Work ids whose candidate waits for its trusted measurement while no agent works on them.
    They are still in flight: a reply may not continue, park or abandon them until the
    measurement ends, so a hypothesis's records stay one round at a time."""
    merge_ids: frozenset[str] = frozenset()
    merging: bool = False
    merge_options: MergeOptions = field(default_factory=MergeOptions)

    def hypothesis(self, identifier: str) -> HypothesisRecord | None:
        """The record of a hypothesis ID, if one exists."""
        return next((item for item in self.hypotheses if item.hypothesis_id == identifier), None)


def _alternatives(offered: tuple[ParentOption, ...]) -> tuple[Alternative, ...]:
    return tuple(
        Alternative(
            hypothesis_id=item.snapshot.hypothesis_id,
            revision=item.snapshot.revision.revision_id.root,
        )
        for item in offered
    )


def _exact_revision(
    path: tuple[str | int, ...],
    plan: WorkstreamPlan,
    mine: tuple[ParentOption, ...],
    offered: tuple[ParentOption, ...],
) -> PlanViolation | None:
    if any(item.snapshot.revision.revision_id.root == plan.parent_revision for item in mine):
        return None
    return PlanViolation(
        path=(*path, "parent_revision"),
        code=ViolationCode.REVISION_NOT_OFFERED,
        subject=str(plan.parent_hypothesis_id),
        revision=str(plan.parent_revision),
        usable=_alternatives(offered),
    )


def _implement_parent(
    position: int, plan: WorkstreamPlan, offered: tuple[ParentOption, ...]
) -> PlanViolation | None:
    path: tuple[str | int, ...] = ("workstreams", position)
    chosen = plan.parent_hypothesis_id
    if chosen is None and plan.parent_revision is not None:
        return PlanViolation(
            path=(*path, "parent_revision"),
            code=ViolationCode.REVISION_WITHOUT_PARENT,
            usable=_alternatives(offered),
        )
    if chosen is None:
        return None
    field = (*path, "parent_hypothesis_id")
    if plan.continue_hypothesis:
        return PlanViolation(path=field, code=ViolationCode.CONTINUATION_HAS_PARENT)
    mine = tuple(item for item in offered if item.snapshot.hypothesis_id == chosen)
    if plan.parent_revision is not None:
        return _exact_revision(path, plan, mine, offered)
    if any(item.latest_verified for item in mine):
        return None
    return PlanViolation(path=field, code=ViolationCode.PARENT_NOT_BUILDABLE, subject=chosen)


def _implement_identity(
    position: int,
    plan: WorkstreamPlan,
    ledger: PlanLedger,
    *,
    abandoned: frozenset[str],
) -> PlanViolation | None:
    identifier = plan.hypothesis_id
    path: tuple[str | int, ...] = ("workstreams", position, "hypothesis_id")
    prior = ledger.hypothesis(identifier)
    code: ViolationCode | None = None
    if identifier in ledger.profile_ids | ledger.merge_ids:
        code = ViolationCode.HYPOTHESIS_ID_USED
    elif identifier in abandoned or (
        prior is not None and prior.strategy is HypothesisStrategy.ABANDONED
    ):
        code = ViolationCode.HYPOTHESIS_ABANDONED
    elif identifier in ledger.running | ledger.submitted:
        code = ViolationCode.HYPOTHESIS_IN_FLIGHT
    elif prior is None and plan.continue_hypothesis:
        code = ViolationCode.HYPOTHESIS_UNKNOWN
    elif prior is not None and not plan.continue_hypothesis:
        code = ViolationCode.HYPOTHESIS_ID_USED
    if code is not None:
        return PlanViolation(path=path, code=code, subject=identifier)
    repairing = ledger.repairable is not None and identifier in ledger.repairable
    return None if prior is None else _continuation(position, plan, prior, repairing=repairing)


def _continuation(
    position: int, plan: WorkstreamPlan, prior: HypothesisRecord, *, repairing: bool
) -> PlanViolation | None:
    """Why ``plan`` may not continue ``prior``; a failed plan step is never terminal."""
    last = prior.rounds[-1] if prior.rounds else None
    if last is None:
        return None
    if last.outcome in _TERMINAL_OUTCOMES and not repairing:
        return PlanViolation(
            path=("workstreams", position, "hypothesis_id"),
            code=ViolationCode.HYPOTHESIS_TERMINAL,
            subject=plan.hypothesis_id,
        )
    if last.outcome is HypothesisOutcome.BLOCKED and plan.task.strip() == prior.last_task.strip():
        return PlanViolation(
            path=("workstreams", position, "task"),
            code=ViolationCode.HYPOTHESIS_BLOCKED,
            subject=plan.hypothesis_id,
        )
    return None


def _profile_violation(
    position: int,
    plan: ProfilePlan,
    ledger: PlanLedger,
    offered: tuple[ParentOption, ...],
    offer: ProfileOffer | None,
) -> PlanViolation | None:
    path: tuple[str | int, ...] = ("workstreams", position)
    if offer is None:
        return PlanViolation(path=(*path, "kind"), code=ViolationCode.PROFILE_NOT_OFFERED)
    unsupported = sorted(set(plan.required_fields) - set(offer.fields))
    if unsupported:
        return PlanViolation(
            path=(*path, "required_fields"),
            code=ViolationCode.PROFILE_FIELDS_UNSUPPORTED,
            fields=tuple(str(item) for item in unsupported),
            supplied=tuple(str(item) for item in offer.fields),
        )
    used = {item.hypothesis_id for item in ledger.hypotheses} | ledger.profile_ids
    if plan.profile_id in used | ledger.merge_ids:
        return PlanViolation(
            path=(*path, "profile_id"), code=ViolationCode.PROFILE_ID_USED, subject=plan.profile_id
        )
    target = plan.target_hypothesis_id
    if target is not None and not any(
        item.snapshot.hypothesis_id == target and item.latest_verified for item in offered
    ):
        return PlanViolation(
            path=(*path, "target_hypothesis_id"),
            code=ViolationCode.PARENT_NOT_BUILDABLE,
            subject=target,
        )
    return None


def _merge_violation(
    position: int,
    plan: MergePlan,
    ledger: PlanLedger,
    offered: tuple[ParentOption, ...],
    *,
    merging: bool,
) -> PlanViolation | None:
    """A merge needs a new ID, no other merge under way, and distinct mergeable candidates.

    A candidate is mergeable by the rule `MergeOptions` holds, which also lists them in the
    planning prompt, so a merge the prompt does not offer is never scheduled.
    """
    path: tuple[str | int, ...] = ("workstreams", position)
    used = {item.hypothesis_id for item in ledger.hypotheses} | ledger.profile_ids
    if plan.merge_id in used | ledger.merge_ids:
        return PlanViolation(
            path=(*path, "merge_id"), code=ViolationCode.MERGE_ID_USED, subject=plan.merge_id
        )
    options = ledger.merge_options
    if not options.checkable:
        return PlanViolation(path=(*path, "kind"), code=ViolationCode.MERGE_UNCHECKABLE)
    if merging:
        return PlanViolation(path=(*path, "kind"), code=ViolationCode.MERGE_IN_FLIGHT)
    seen: set[str] = set()
    for index, candidate in enumerate(plan.candidates):
        field = (*path, "candidates", index)
        if candidate in seen:
            return PlanViolation(
                path=field, code=ViolationCode.MERGE_CANDIDATE_REPEATED, subject=candidate
            )
        seen.add(candidate)
        if options.mergeable(candidate):
            continue
        code = options.bar(candidate)
        if code is None or not any(
            item.snapshot.hypothesis_id == candidate and item.latest_verified for item in offered
        ):
            code = ViolationCode.PARENT_NOT_BUILDABLE
        return PlanViolation(path=field, code=code, subject=candidate)
    return None


def _combine_violation(
    position: int, plan: CombinePlan, ledger: PlanLedger, offered: tuple[ParentOption, ...]
) -> PlanViolation | None:
    """A combine needs a new hypothesis ID and distinct parts, each an offered exact revision.

    There is no bar against main: parts that lost or tied alone are what a combine is for.
    """
    path: tuple[str | int, ...] = ("workstreams", position)
    identifier = plan.hypothesis_id
    taken = (
        {item.hypothesis_id for item in ledger.hypotheses}
        | ledger.profile_ids
        | ledger.merge_ids
        | ledger.running
        | ledger.submitted
    )
    if identifier in taken:
        return PlanViolation(
            path=(*path, "hypothesis_id"), code=ViolationCode.HYPOTHESIS_ID_USED, subject=identifier
        )
    seen: set[str] = set()
    for index, part in enumerate(plan.parents):
        field = (*path, "parents", index)
        if part.hypothesis_id in seen:
            return PlanViolation(
                path=(*field, "hypothesis_id"),
                code=ViolationCode.COMBINE_PARENT_REPEATED,
                subject=part.hypothesis_id,
            )
        seen.add(part.hypothesis_id)
        mine = tuple(item for item in offered if item.snapshot.hypothesis_id == part.hypothesis_id)
        if not mine:
            return PlanViolation(
                path=(*field, "hypothesis_id"),
                code=ViolationCode.PARENT_NOT_BUILDABLE,
                subject=part.hypothesis_id,
            )
        if not any(item.snapshot.revision.revision_id.root == part.revision for item in mine):
            return PlanViolation(
                path=(*field, "revision"),
                code=ViolationCode.REVISION_NOT_OFFERED,
                subject=part.hypothesis_id,
                revision=part.revision,
                usable=_alternatives(offered),
            )
    return None


def _updates(
    plan: PortfolioPlan, ledger: PlanLedger
) -> tuple[tuple[PlanUpdate, ...], tuple[PlanViolation, ...]]:
    kept: list[PlanUpdate] = []
    bad: list[PlanViolation] = []
    seen: set[str] = set()
    for position, update in enumerate(plan.hypothesis_updates):
        path = ("hypothesis_updates", position, "hypothesis_id")
        record = ledger.hypothesis(update.hypothesis_id)
        if update.hypothesis_id in ledger.running | ledger.submitted:
            code = ViolationCode.UPDATE_RUNNING
        elif update.hypothesis_id in seen:
            code = ViolationCode.UPDATE_DUPLICATE
        elif record is None:
            code = ViolationCode.UPDATE_UNKNOWN
        elif not record.rounds:
            code = ViolationCode.UPDATE_INCOMPLETE
        else:
            seen.add(update.hypothesis_id)
            kept.append(
                PlanUpdate(
                    hypothesis_id=update.hypothesis_id,
                    disposition=_RETIREMENTS[update.disposition],
                    reason=update.reason,
                    reason_kind=update.reason_kind,
                )
            )
            continue
        bad.append(PlanViolation(path=path, code=code, subject=update.hypothesis_id))
    return tuple(kept), tuple(bad)


def _ranking(
    plan: PortfolioPlan, ledger: PlanLedger
) -> tuple[tuple[str, ...] | None, tuple[PlanViolation, ...]]:
    """The reply's order of evaluations, when it gave one the run may take."""
    order = plan.evaluation_order
    if not order or not ledger.ranks:
        return None, ()  # a run that picks the newest reads no order, so none is wrong
    known = (
        {item.hypothesis_id for item in ledger.hypotheses}
        | ledger.profile_ids
        | ledger.merge_ids
        | {planned_id(entry) for entry in plan.workstreams}
    )
    bad: list[PlanViolation] = []
    seen: set[str] = set()
    for position, identifier in enumerate(order):
        path = ("evaluation_order", position)
        if identifier in seen:
            bad.append(
                PlanViolation(path=path, code=ViolationCode.RANKING_DUPLICATE, subject=identifier)
            )
        elif identifier not in known:
            bad.append(
                PlanViolation(path=path, code=ViolationCode.RANKING_UNKNOWN, subject=identifier)
            )
        seen.add(identifier)
    return (None, tuple(bad)) if bad else (order, ())


type _Entry = WorkstreamPlan | ProfilePlan | MergePlan | CombinePlan


def _work_plan(entry: _Entry) -> WorkPlan:
    if isinstance(entry, CombinePlan):
        return WorkPlan(
            kind=WorkKind.IMPLEMENT,
            work_id=entry.hypothesis_id,
            title=entry.title,
            hypothesis=entry.hypothesis,
            task=entry.instructions,
            pass_criteria=entry.pass_criteria,
            watch_for=entry.watch_for,
            combine=tuple(
                CombinePick(hypothesis_id=part.hypothesis_id, revision=part.revision)
                for part in entry.parents
            ),
        )
    if isinstance(entry, MergePlan):
        return WorkPlan(
            kind=WorkKind.MERGE,
            work_id=entry.merge_id,
            candidates=entry.candidates,
            instructions=entry.instructions,
            watch_for=entry.watch_for,
        )
    if isinstance(entry, ProfilePlan):
        return WorkPlan(
            kind=WorkKind.PROFILE,
            work_id=entry.profile_id,
            parent_hypothesis_id=entry.target_hypothesis_id,
            question=entry.question,
            required_fields=tuple(sorted(str(item) for item in entry.required_fields)),
            decision_impact=entry.decision_impact or "",
        )
    return WorkPlan(
        kind=WorkKind.IMPLEMENT,
        work_id=entry.hypothesis_id,
        title=entry.title,
        hypothesis=entry.hypothesis,
        task=entry.task,
        pass_criteria=entry.pass_criteria,
        continue_hypothesis=entry.continue_hypothesis,
        evidence=tuple(item.location for item in entry.evidence),
        parent_hypothesis_id=entry.parent_hypothesis_id,
        parent_revision=entry.parent_revision,
    )


def _repairs(entry: _Entry, repairable: frozenset[str]) -> bool:
    """Whether ``entry`` continues one of the failed plan steps; a merge or combine never does."""
    return (
        isinstance(entry, WorkstreamPlan)
        and entry.continue_hypothesis
        and entry.hypothesis_id in repairable
    )


def validate(
    plan: PortfolioPlan,
    ledger: PlanLedger,
    offered: tuple[ParentOption, ...],
    *,
    capacity: int,
    offer: ProfileOffer | None,
) -> PlanCheck:
    """Split ``plan`` into schedulable entries and typed violations, never raising."""
    updates, bad = _updates(plan, ledger)
    ranking, misranked = _ranking(plan, ledger)
    abandoned = frozenset(item.hypothesis_id for item in updates if item.disposition == "abandoned")
    violations = [*bad, *misranked]
    if len(plan.workstreams) > capacity:
        violations.append(
            PlanViolation(
                path=("workstreams",),
                code=ViolationCode.OVER_CAPACITY,
                requested=len(plan.workstreams),
                capacity=capacity,
            )
        )
    accepted: list[WorkPlan] = []
    seen: set[str] = set()
    merging = ledger.merging
    for position, entry in enumerate(plan.workstreams):
        identifier = planned_id(entry)
        violation: PlanViolation | None
        if identifier in seen:
            violation = PlanViolation(
                path=("workstreams", position),
                code=ViolationCode.DUPLICATE_ID,
                subject=identifier,
            )
        elif ledger.repairable is not None and not _repairs(entry, ledger.repairable):
            violation = PlanViolation(
                path=("workstreams", position), code=ViolationCode.OUTSIDE_PLAN, subject=identifier
            )
        elif isinstance(entry, ProfilePlan):
            violation = _profile_violation(position, entry, ledger, offered, offer)
        elif isinstance(entry, MergePlan):
            violation = _merge_violation(position, entry, ledger, offered, merging=merging)
            merging = merging or violation is None  # one merge at a time: main moves once
        elif isinstance(entry, CombinePlan):
            violation = _combine_violation(position, entry, ledger, offered)
        else:
            violation = _implement_identity(
                position, entry, ledger, abandoned=abandoned
            ) or _implement_parent(position, entry, offered)
        seen.add(identifier)
        if violation is not None:
            violations.append(violation)
        elif len(accepted) < capacity:
            accepted.append(_work_plan(entry))
    note, unknown = _handoff_note(plan, accepted)
    return PlanCheck(
        reasoning=plan.reasoning,
        handoff=note,
        accepted=tuple(accepted),
        updates=updates,
        violations=(*violations, *unknown),
        ranking=ranking,
    )


def _handoff_note(
    plan: PortfolioPlan, accepted: list[WorkPlan]
) -> tuple[HandoffNote, tuple[PlanViolation, ...]]:
    """The reply's note cut to the scheduled workstreams, and its expectations of no workstream.

    An expectation of a workstream that was not scheduled is dropped (its ID is kept in
    ``dropped``), so the handoff file never tells the next planner what to do after work that
    never ran; one that names no workstream of the reply is also a violation to correct.
    """
    scheduled = {item.work_id for item in accepted}
    replied = {planned_id(entry) for entry in plan.workstreams}
    kept: list[Expectation] = []
    dropped: list[str] = []
    unknown: list[PlanViolation] = []
    for position, item in enumerate(plan.handoff.expectations):
        if item.work_id in scheduled:
            kept.append(Expectation.model_validate(item.model_dump()))
            continue
        dropped.append(item.work_id)
        if item.work_id not in replied:
            unknown.append(
                PlanViolation(
                    path=("handoff", "expectations", position, "work_id"),
                    code=ViolationCode.EXPECTATION_UNKNOWN,
                    subject=item.work_id,
                )
            )
    note = HandoffNote(expectations=tuple(kept), doubts=plan.handoff.doubts, dropped=tuple(dropped))
    return note, tuple(unknown)
