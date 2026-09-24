"""Persistence boundary for the unified agent-loop state aggregate."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from vibesys.loops.agent.hypotheses import (
    apply_strategy_updates,
    project_round_evidence,
    reproject_run_evidence,
)
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisMeasurement,
    HypothesisResolution,
    HypothesisReview,
    HypothesisStrategy,
)
from vibesys.loops.metrics import MetricSpace
from vibesys.schemas import (
    CandidateDisposition,
    HypothesisOutcome,
    OrchestratorPlan,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_loop_state.api import RoundRecord
    from vs_project.api import StateNamespace, StateSlot, StateTransition


class AgentRunStateStore:
    """Persist the single authoritative agent-loop state file."""

    _STATE_FILE = "state.json"
    _LEGACY_LEDGER_FILE = "hypotheses.json"
    _LEGACY_ACTIVE_FILE = "active.json"

    def __init__(self, namespace: StateNamespace) -> None:
        """Bind the store to one run's portable agent namespace."""
        self._namespace = namespace
        self._slot: StateSlot[AgentRunState] = namespace.slot(
            self._STATE_FILE,
            AgentRunState,
        )

    def load_optional(self) -> AgentRunState | None:
        """Load unified state, returning ``None`` only when absent."""
        return self._slot.load_optional()

    def load(self) -> AgentRunState:
        """Load unified state or return a new empty aggregate."""
        return self.load_optional() or AgentRunState()

    def save(self, state: AgentRunState) -> None:
        """Atomically replace the unified state."""
        self._slot.save(state)

    def transition(self, state: AgentRunState) -> StateTransition:
        """Prepare an exact replacement without applying it."""
        return self._slot.transition(state)

    def apply_transition(self, transition: StateTransition) -> None:
        """Validate and apply a previously prepared state transition."""
        self._slot.apply(transition)

    def migrate_legacy(
        self,
        *,
        rounds: Sequence[RoundRecord],
        local_namespace: StateNamespace,
        legacy_space: MetricSpace,
    ) -> AgentRunState:
        """Build unified state from legacy files without modifying either format.

        An existing ``state.json`` carries the run's own metric space and
        answers for itself. *legacy_space* applies only where there is none:
        unified state written before the space was persisted, and the older
        ledger and active-checkpoint files. The loop passes the space the task
        declares and adopts it immediately after this call; the server, which
        cannot read the task directory, passes the axes recorded in the run
        manifest, which carries no tolerance and therefore compares exactly.
        """
        existing = self.load_optional()
        if existing is not None:
            return reproject_run_evidence(_with_legacy_space(existing, legacy_space))
        ledger = self._namespace.load_optional(
            self._LEGACY_LEDGER_FILE,
            _LegacyHypothesisLedger,
        )
        active = local_namespace.load_optional(
            self._LEGACY_ACTIVE_FILE,
            _LegacyActiveHypothesis,
        )
        return _migrate_legacy_state(
            rounds=rounds,
            ledger=ledger,
            active=active,
            legacy_space=legacy_space,
        )

    def cleanup_legacy(
        self,
        *,
        round_numbers: Sequence[int],
        local_namespace: StateNamespace,
    ) -> None:
        """Delete old files after the caller has committed ``state.json``."""
        self.cleanup_legacy_portable(round_numbers)
        self.cleanup_legacy_local(local_namespace)

    def cleanup_legacy_portable(self, round_numbers: Sequence[int]) -> None:
        """Remove legacy portable documents before an exact namespace commit."""
        unique_rounds = sorted(set(round_numbers))
        if any(number <= 0 for number in unique_rounds):
            raise ValueError("legacy round numbers must be positive")
        self._namespace.apply(self._namespace.transition(self._LEGACY_LEDGER_FILE, None))
        for number in unique_rounds:
            self._namespace.apply(self._namespace.transition(f"rounds/{number:04d}.json", None))

    def cleanup_legacy_local(self, local_namespace: StateNamespace) -> None:
        """Remove the obsolete machine-local active checkpoint."""
        local_namespace.apply(local_namespace.transition(self._LEGACY_ACTIVE_FILE, None))

    @property
    def namespace(self) -> StateNamespace:
        """Return the namespace used for durable Git snapshots."""
        return self._namespace


def _with_legacy_space(state: AgentRunState, legacy_space: MetricSpace) -> AgentRunState:
    """Supply a space to unified state written before one was persisted."""
    if state.metrics != MetricSpace():
        return state
    return state.model_copy(update={"metrics": legacy_space}, deep=True)


class _LegacyHypothesisStrategy(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    PARKED = "parked"
    ABANDONED = "abandoned"


class _LegacyHypothesisRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hypothesis_id: str = Field(min_length=1)
    claim: str | None = None
    task: str | None = None
    started_round: Annotated[int, Field(gt=0)]
    rounds: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    parent_round: Annotated[int, Field(gt=0)] | None = None
    parent_commit: str | None = None
    declared_outcome: HypothesisOutcome | None = None
    review: HypothesisReview = HypothesisReview.PENDING
    resolution: HypothesisResolution | None = None
    measurement: HypothesisMeasurement | None = None
    candidate_retained: bool | None = None
    strategy: _LegacyHypothesisStrategy = _LegacyHypothesisStrategy.ACTIVE
    strategy_reason: str | None = None


class _LegacyHypothesisLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    hypotheses: list[_LegacyHypothesisRecord] = Field(default_factory=list)


class _LegacyActiveHypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    plan: OrchestratorPlan
    started_round: Annotated[int, Field(gt=0)]
    parent_round: Annotated[int, Field(gt=0)] | None = None
    parent_commit: str | None = None
    feedback: str | None = None
    next_step: str | None = None
    continuation_rounds: Annotated[int, Field(ge=0)] = 0
    revert_applied: bool = False
    revert_commit: str | None = None
    gate_revalidation_pending: bool = False
    gate_approved_perf_metric: FiniteFloat | None = None
    gate_approved_perf_unit: str | None = None
    gate_approved_metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    gate_approved_evaluation_artifact: str | None = None
    gate_approved_candidate_disposition: str = CandidateDisposition.UNASSESSED.value
    gate_approved_candidate_metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    gate_approved_candidate_evaluation_artifact: str | None = None
    gate_approved_candidate_operating_point: str = ""
    gate_approved_candidate_retention_reason: str = ""
    gate_candidate_commit: str | None = None
    gate_accuracy_passed: bool = False


def _migrate_legacy_state(
    *,
    rounds: Sequence[RoundRecord],
    ledger: _LegacyHypothesisLedger | None,
    active: _LegacyActiveHypothesis | None,
    legacy_space: MetricSpace,
) -> AgentRunState:
    ordered_rounds = sorted(rounds, key=lambda record: record.round_number)
    records_by_id = {
        record.hypothesis_id: record for record in (ledger.hypotheses if ledger is not None else ())
    }
    identifiers: list[str] = []
    for record in ordered_rounds:
        identifier = record.hypothesis_id or _legacy_unassigned_id(record.round_number)
        if identifier not in identifiers:
            identifiers.append(identifier)
    for record in ledger.hypotheses if ledger is not None else ():
        if record.hypothesis_id not in identifiers:
            identifiers.append(record.hypothesis_id)
    if active is not None and active.plan.hypothesis_id not in identifiers:
        identifiers.append(active.plan.hypothesis_id)

    hypotheses = [
        _legacy_hypothesis(
            identifier,
            rounds=ordered_rounds,
            record=records_by_id.get(identifier),
            active=active,
        )
        for identifier in identifiers
    ]
    state = AgentRunState(metrics=legacy_space, hypotheses=hypotheses)
    prior_rounds: list[RoundRecord] = []
    for round_record in ordered_rounds:
        identifier = round_record.hypothesis_id or _legacy_unassigned_id(round_record.round_number)
        normalized_record = (
            round_record
            if round_record.hypothesis_id is not None
            else replace(round_record, hypothesis_id=identifier)
        )
        hypothesis = state.by_id(identifier)
        assert hypothesis is not None  # created above
        projected = project_round_evidence(
            hypothesis,
            normalized_record,
            prior_rounds=prior_rounds,
            space=state.metrics,
        )
        index = next(
            index for index, item in enumerate(state.hypotheses) if item.hypothesis_id == identifier
        )
        state.hypotheses[index] = projected
        prior_rounds.append(normalized_record)

    if active is not None:
        state = apply_strategy_updates(state, active.plan.hypothesis_updates)
    active_identifier = _legacy_active_id(active)
    if active_identifier is not None and state.by_id(active_identifier) is not None:
        active_index = next(
            index
            for index, hypothesis in enumerate(state.hypotheses)
            if hypothesis.hypothesis_id == active_identifier
        )
        active_hypothesis = state.hypotheses[active_index]
        active_hypothesis.strategy = HypothesisStrategy.AVAILABLE
        active_hypothesis.strategy_reason = None
        state.active_hypothesis_id = active_identifier
    return AgentRunState.model_validate(state.model_dump())


def _legacy_hypothesis(
    identifier: str,
    *,
    rounds: Sequence[RoundRecord],
    record: _LegacyHypothesisRecord | None,
    active: _LegacyActiveHypothesis | None,
) -> Hypothesis:
    matching = [
        item
        for item in rounds
        if (item.hypothesis_id or _legacy_unassigned_id(item.round_number)) == identifier
    ]
    active_match = (
        active if active is not None and active.plan.hypothesis_id == identifier else None
    )
    first = matching[0] if matching else None
    started_round = (
        active_match.started_round
        if active_match is not None
        else record.started_round
        if record is not None
        else first.round_number
        if first is not None
        else 1
    )
    plan = (
        active_match.plan.model_copy(deep=True)
        if active_match is not None
        else _legacy_plan(identifier, record=record, first=first)
    )
    hypothesis = Hypothesis(
        hypothesis_id=identifier,
        plan=plan,
        started_round=started_round,
        parent_round=(
            active_match.parent_round
            if active_match is not None
            else record.parent_round
            if record is not None
            else first.hypothesis_parent_round
            if first is not None
            else None
        ),
        parent_commit=(
            active_match.parent_commit
            if active_match is not None
            else record.parent_commit
            if record is not None
            else first.hypothesis_parent_commit
            if first is not None
            else None
        ),
        strategy=_legacy_strategy(record),
        strategy_reason=record.strategy_reason if record is not None else None,
    )
    if record is not None:
        hypothesis.declared_outcome = record.declared_outcome
        hypothesis.review = record.review
        hypothesis.resolution = record.resolution
        hypothesis.measurement = record.measurement
        hypothesis.candidate_retained = record.candidate_retained
    if active_match is not None:
        for field in _LEGACY_OPERATIONAL_FIELDS:
            setattr(hypothesis, field, getattr(active_match, field))
    return hypothesis


_LEGACY_OPERATIONAL_FIELDS = (
    "feedback",
    "next_step",
    "continuation_rounds",
    "revert_applied",
    "revert_commit",
    "gate_revalidation_pending",
    "gate_approved_perf_metric",
    "gate_approved_perf_unit",
    "gate_approved_metrics",
    "gate_approved_evaluation_artifact",
    "gate_approved_candidate_disposition",
    "gate_approved_candidate_metrics",
    "gate_approved_candidate_evaluation_artifact",
    "gate_approved_candidate_operating_point",
    "gate_approved_candidate_retention_reason",
    "gate_candidate_commit",
    "gate_accuracy_passed",
)


def _legacy_plan(
    identifier: str,
    *,
    record: _LegacyHypothesisRecord | None,
    first: RoundRecord | None,
) -> OrchestratorPlan:
    claim = record.claim if record is not None else first.hypothesis_claim if first else None
    task = record.task if record is not None else first.hypothesis_task if first else None
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=claim or "",
        task=task or "",
        pass_criteria="",
        reasoning="",
    )


def _legacy_strategy(record: _LegacyHypothesisRecord | None) -> HypothesisStrategy:
    if record is None:
        return HypothesisStrategy.AVAILABLE
    if record.strategy is _LegacyHypothesisStrategy.PARKED:
        return HypothesisStrategy.PARKED
    if record.strategy is _LegacyHypothesisStrategy.ABANDONED:
        return HypothesisStrategy.ABANDONED
    return HypothesisStrategy.AVAILABLE


def _legacy_active_id(active: _LegacyActiveHypothesis | None) -> str | None:
    """Trust only the restart checkpoint to identify legacy active work."""
    return active.plan.hypothesis_id if active is not None else None


def _legacy_unassigned_id(round_number: int) -> str:
    return f"legacy-round-{round_number}"
