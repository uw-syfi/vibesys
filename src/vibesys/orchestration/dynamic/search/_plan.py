"""Pure validation of a planner portfolio against what the search has recorded.

Every rejection is a typed `PlanViolation` naming the offending plan field, so the planner
correction can quote it from a template. Validation never raises: it splits a portfolio into the
entries that may be scheduled and the violations that explain the rest.
"""

from dataclasses import dataclass
from typing import Literal

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.orchestration.dynamic.models import (
    PortfolioPlan,
    ProfileOffer,
    ProfilePlan,
    StrategyReason,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.search._parents import ParentOption
from vibesys.orchestration.dynamic.search._values import (
    Alternative,
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

    @property
    def valid(self) -> bool:
        """Whether the whole portfolio was accepted unchanged."""
        return not self.violations


@dataclass(frozen=True)
class PlanLedger:
    """What validation reads of the search: hypotheses, what is running and which IDs are profiles."""

    hypotheses: tuple[HypothesisRecord, ...]
    running: frozenset[str]
    profile_ids: frozenset[str]

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
    if identifier in ledger.profile_ids:
        code = ViolationCode.HYPOTHESIS_ID_USED
    elif identifier in abandoned or (
        prior is not None and prior.strategy is HypothesisStrategy.ABANDONED
    ):
        code = ViolationCode.HYPOTHESIS_ABANDONED
    elif identifier in ledger.running:
        code = ViolationCode.HYPOTHESIS_IN_FLIGHT
    elif prior is None and plan.continue_hypothesis:
        code = ViolationCode.HYPOTHESIS_UNKNOWN
    elif prior is not None and not plan.continue_hypothesis:
        code = ViolationCode.HYPOTHESIS_ID_USED
    if code is not None:
        return PlanViolation(path=path, code=code, subject=identifier)
    return None if prior is None else _continuation(position, plan, prior)


def _continuation(
    position: int, plan: WorkstreamPlan, prior: HypothesisRecord
) -> PlanViolation | None:
    last = prior.rounds[-1] if prior.rounds else None
    if last is None:
        return None
    if last.outcome in _TERMINAL_OUTCOMES:
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
    if plan.profile_id in used:
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


def _updates(
    plan: PortfolioPlan, ledger: PlanLedger
) -> tuple[tuple[PlanUpdate, ...], tuple[PlanViolation, ...]]:
    kept: list[PlanUpdate] = []
    bad: list[PlanViolation] = []
    seen: set[str] = set()
    for position, update in enumerate(plan.hypothesis_updates):
        path = ("hypothesis_updates", position, "hypothesis_id")
        record = ledger.hypothesis(update.hypothesis_id)
        if update.hypothesis_id in ledger.running:
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


def _work_plan(entry: WorkstreamPlan | ProfilePlan) -> WorkPlan:
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
    abandoned = frozenset(item.hypothesis_id for item in updates if item.disposition == "abandoned")
    violations = list(bad)
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
    for position, entry in enumerate(plan.workstreams):
        identifier = planned_id(entry)
        violation: PlanViolation | None
        if identifier in seen:
            violation = PlanViolation(
                path=("workstreams", position),
                code=ViolationCode.DUPLICATE_ID,
                subject=identifier,
            )
        elif isinstance(entry, ProfilePlan):
            violation = _profile_violation(position, entry, ledger, offered, offer)
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
