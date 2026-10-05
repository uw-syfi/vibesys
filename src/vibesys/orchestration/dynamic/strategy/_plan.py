"""Pure validation of a planner portfolio against the strategy's scientific state.

Every rejection is a typed `PlanViolation` naming the offending plan field, so the
planner correction can quote it. Messages keep the wording of the legacy loop's
`DynamicPlanError` so one correction template serves both. Validation never
raises: it splits a portfolio into the entries that may be scheduled and the
violations that explain the rest.
"""

from typing import Literal

from vibesys.hypothesis import HypothesisOutcome, HypothesisStrategy
from vibesys.orchestration.dynamic.models import (
    PortfolioPlan,
    ProfilePlan,
    WorkstreamPlan,
    planned_id,
)
from vibesys.orchestration.dynamic.strategy._parents import ParentOption
from vibesys.orchestration.dynamic.strategy._state import (
    DynamicStrategyState,
    HypothesisRecord,
    WorkKind,
    WorkPhase,
    WorkPlan,
)
from vs_core.api import Value

_TERMINAL_OUTCOMES = frozenset(
    {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED, HypothesisOutcome.DISPROVEN}
)


class PlanViolation(Value):
    """One reason a planned entry or update cannot be applied."""

    path: str
    detail: str

    def render(self) -> str:
        """The correction line shown to the planner."""
        return f"{self.path}: {self.detail}"


class PlanUpdate(Value):
    """A validated park or abandon decision for a finished hypothesis."""

    hypothesis_id: str
    disposition: Literal["parked", "abandoned"]
    reason: str
    reason_kind: str


class PlanCheck(Value):
    """The schedulable part of a portfolio and the violations that excluded the rest."""

    accepted: tuple[WorkPlan, ...] = ()
    updates: tuple[PlanUpdate, ...] = ()
    violations: tuple[PlanViolation, ...] = ()

    @property
    def valid(self) -> bool:
        """Whether the whole portfolio was accepted unchanged."""
        return not self.violations


def in_flight(state: DynamicStrategyState) -> frozenset[str]:
    """Work IDs of workstreams that have not finished."""
    return frozenset(
        item.plan.work_id for item in state.attempts if item.phase is not WorkPhase.DONE
    )


def _hypothesis(state: DynamicStrategyState, identifier: str) -> HypothesisRecord | None:
    return next((item for item in state.hypotheses if item.hypothesis_id == identifier), None)


def _profile_ids(state: DynamicStrategyState) -> frozenset[str]:
    return frozenset(
        item.plan.work_id for item in state.attempts if item.plan.kind is WorkKind.PROFILE
    )


def _alternatives(offered: tuple[ParentOption, ...]) -> tuple[tuple[str, str], ...]:
    return tuple(
        (item.snapshot.hypothesis_id, item.snapshot.revision.revision_id.root) for item in offered
    )


def _exact_revision(
    path: str,
    plan: WorkstreamPlan,
    mine: tuple[ParentOption, ...],
    offered: tuple[ParentOption, ...],
) -> PlanViolation | None:
    if any(item.snapshot.revision.revision_id.root == plan.parent_revision for item in mine):
        return None
    return PlanViolation(
        path=f"{path}.parent_revision",
        detail=(
            f"{plan.parent_revision!r} is not an offered revision of "
            f"{plan.parent_hypothesis_id!r}; usable options: {_alternatives(offered)}"
        ),
    )


def _implement_parent(
    position: int, plan: WorkstreamPlan, offered: tuple[ParentOption, ...]
) -> PlanViolation | None:
    path = f"workstreams[{position}]"
    chosen = plan.parent_hypothesis_id
    field = f"{path}.parent_hypothesis_id"
    if chosen is None and plan.parent_revision is not None:
        return PlanViolation(
            path=f"{path}.parent_revision",
            detail=(
                "an exact candidate revision requires parent_hypothesis_id; usable options: "
                f"{_alternatives(offered)}"
            ),
        )
    if chosen is None:
        return None
    if plan.continue_hypothesis:
        return PlanViolation(
            path=field, detail="a continued hypothesis builds on its own candidate; use null"
        )
    mine = tuple(item for item in offered if item.snapshot.hypothesis_id == chosen)
    if plan.parent_revision is not None:
        return _exact_revision(path, plan, mine, offered)
    if any(item.latest_verified for item in mine):
        return None
    return PlanViolation(
        path=field,
        detail=(
            f"{chosen!r} is not a buildable candidate; name one listed under buildable "
            "candidates, or use null for the base revision"
        ),
    )


def _implement_identity(
    position: int,
    plan: WorkstreamPlan,
    state: DynamicStrategyState,
    *,
    abandoned: frozenset[str],
    running: frozenset[str],
) -> PlanViolation | None:
    identifier = plan.hypothesis_id
    path = f"workstreams[{position}].hypothesis_id"
    prior = _hypothesis(state, identifier)
    if identifier in _profile_ids(state):
        return PlanViolation(path=path, detail=f"hypothesis ID {identifier!r} was already used")
    if identifier in abandoned or (
        prior is not None and prior.strategy is HypothesisStrategy.ABANDONED
    ):
        return PlanViolation(
            path=path, detail=f"{identifier!r} is abandoned and cannot be continued"
        )
    if identifier in running:
        return PlanViolation(path=path, detail=f"hypothesis {identifier!r} is still in flight")
    if prior is None and plan.continue_hypothesis:
        return PlanViolation(
            path=path, detail=f"unknown hypothesis {identifier!r} cannot be continued"
        )
    if prior is not None and not plan.continue_hypothesis:
        return PlanViolation(path=path, detail=f"hypothesis ID {identifier!r} was already used")
    return None if prior is None else _continuation(position, plan, prior)


def _continuation(
    position: int, plan: WorkstreamPlan, prior: HypothesisRecord
) -> PlanViolation | None:
    last = prior.rounds[-1] if prior.rounds else None
    if last is None:
        return None
    if last.outcome in _TERMINAL_OUTCOMES:
        return PlanViolation(
            path=f"workstreams[{position}].hypothesis_id",
            detail=f"evaluated hypothesis {plan.hypothesis_id!r} is already terminal",
        )
    if last.outcome is HypothesisOutcome.BLOCKED and plan.task.strip() == prior.last_task.strip():
        return PlanViolation(
            path=f"workstreams[{position}].task",
            detail=(
                f"hypothesis {plan.hypothesis_id!r} was blocked; continue it only with a task "
                "that removes the recorded blocker, or park or abandon it"
            ),
        )
    return None


def _profile_violation(
    position: int,
    plan: ProfilePlan,
    state: DynamicStrategyState,
    offered: tuple[ParentOption, ...],
    *,
    profiling: bool,
) -> PlanViolation | None:
    path = f"workstreams[{position}]"
    if not profiling:
        return PlanViolation(
            path=f"{path}.kind",
            detail=(
                "this run cannot produce trusted profile evidence; schedule only implement "
                "workstreams"
            ),
        )
    used = {item.hypothesis_id for item in state.hypotheses} | _profile_ids(state)
    if plan.profile_id in used:
        return PlanViolation(
            path=f"{path}.profile_id",
            detail=f"{plan.profile_id!r} was already used; choose a new ID",
        )
    target = plan.target_hypothesis_id
    if target is not None and not any(
        item.snapshot.hypothesis_id == target and item.latest_verified for item in offered
    ):
        return PlanViolation(
            path=f"{path}.target_hypothesis_id",
            detail=(
                f"{target!r} is not a buildable candidate; name one listed under buildable "
                "candidates, or use null for the base revision"
            ),
        )
    return None


def _updates(
    plan: PortfolioPlan, state: DynamicStrategyState, running: frozenset[str]
) -> tuple[tuple[PlanUpdate, ...], tuple[PlanViolation, ...]]:
    kept: list[PlanUpdate] = []
    bad: list[PlanViolation] = []
    seen: set[str] = set()
    for position, update in enumerate(plan.hypothesis_updates):
        path = f"hypothesis_updates[{position}].hypothesis_id"
        record = _hypothesis(state, update.hypothesis_id)
        if update.hypothesis_id in running:
            detail = (
                f"{update.hypothesis_id!r} is still running; park or abandon it only after "
                "its workstream finishes"
            )
        elif update.hypothesis_id in seen:
            detail = f"duplicate strategy update for hypothesis {update.hypothesis_id!r}"
        elif record is None:
            detail = f"strategy update names unknown hypothesis {update.hypothesis_id!r}"
        elif not record.rounds:
            detail = f"cannot update incomplete hypothesis {update.hypothesis_id!r}"
        else:
            seen.add(update.hypothesis_id)
            kept.append(
                PlanUpdate(
                    hypothesis_id=update.hypothesis_id,
                    disposition=update.disposition,
                    reason=update.reason,
                    reason_kind=update.reason_kind.value,
                )
            )
            continue
        bad.append(PlanViolation(path=path, detail=detail))
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
    state: DynamicStrategyState,
    offered: tuple[ParentOption, ...],
    *,
    capacity: int,
    profiling: bool,
) -> PlanCheck:
    """Split ``plan`` into schedulable entries and typed violations, never raising."""
    running = in_flight(state)
    updates, bad = _updates(plan, state, running)
    abandoned = frozenset(item.hypothesis_id for item in updates if item.disposition == "abandoned")
    violations = list(bad)
    if len(plan.workstreams) > capacity:
        violations.append(
            PlanViolation(
                path="workstreams",
                detail=(
                    f"portfolio requested {len(plan.workstreams)} workstreams, "
                    f"but capacity is {capacity}"
                ),
            )
        )
    accepted: list[WorkPlan] = []
    seen: set[str] = set()
    for position, entry in enumerate(plan.workstreams):
        identifier = planned_id(entry)
        violation: PlanViolation | None
        if identifier in seen:
            violation = PlanViolation(
                path=f"workstreams[{position}]",
                detail=(
                    f"{identifier!r} repeats an earlier entry's ID; merge the entries or give "
                    "each its own ID"
                ),
            )
        elif isinstance(entry, ProfilePlan):
            violation = _profile_violation(position, entry, state, offered, profiling=profiling)
        else:
            violation = _implement_identity(
                position, entry, state, abandoned=abandoned, running=running
            ) or _implement_parent(position, entry, offered)
        seen.add(identifier)
        if violation is not None:
            violations.append(violation)
        elif len(accepted) < capacity:
            accepted.append(_work_plan(entry))
    return PlanCheck(accepted=tuple(accepted), updates=updates, violations=tuple(violations))
