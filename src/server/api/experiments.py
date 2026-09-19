"""Project authoritative agent-run state into experiment-log protocol entries.

The agent loop owns hypothesis lifecycle state. This module is deliberately a
one-way projection: it does not group rounds, select a baseline, or infer a
resolution from individual round fields.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from server.api.protocol import ExperimentCursor, ExperimentUpdate, HypothesisEntry, HypothesisRound
from vibesys.loops.agent.hypotheses import measurement_delta_reason
from vibesys.loops.agent.model import HypothesisResolution
from vibesys.schemas import CandidateDisposition, HypothesisOutcome, derive_hypothesis_title

if TYPE_CHECKING:
    from vibesys.loops.agent.model import AgentRunState, Hypothesis
    from vs_loop_state import RoundRecord


def build_experiment_log(state: AgentRunState) -> list[HypothesisEntry]:
    """Return the complete hypothesis history in stable start-round order."""
    return sorted(
        (
            build_experiment_entry(hypothesis, active_id=state.active_hypothesis_id)
            for hypothesis in state.hypotheses
        ),
        key=lambda entry: (entry.first_round, entry.hypothesis_id),
    )


def build_experiment_entry(hypothesis: Hypothesis, *, active_id: str | None) -> HypothesisEntry:
    """Copy one domain hypothesis into its presentation-neutral DTO."""
    rounds = hypothesis.rounds
    measurement = hypothesis.measurement
    return HypothesisEntry(
        hypothesis_id=hypothesis.hypothesis_id,
        title=_text(hypothesis.plan.title) or derive_hypothesis_title(hypothesis.plan.hypothesis),
        claim=_text(hypothesis.plan.hypothesis),
        action=_text(hypothesis.plan.task),
        first_round=hypothesis.started_round,
        last_round=rounds[-1].round_number if rounds else hypothesis.started_round,
        rounds=[_round(record) for record in rounds],
        resolved_outcome=(
            hypothesis.resolution.value if hypothesis.resolution is not None else None
        ),
        judge_verdict=_judge_verdict(hypothesis),
        # The measurement fields are intentionally copied as one tuple.
        # Choosing a newer per-round metric here would pair it with a
        # different causal delta and make the UI lie about the measurement.
        perf_metric=measurement.value if measurement is not None else None,
        perf_unit=_text(measurement.unit) if measurement is not None else None,
        perf_delta_pct=measurement.delta_pct if measurement is not None else None,
        perf_metric_name=_text(measurement.metric) if measurement is not None else None,
        perf_direction=measurement.direction if measurement is not None else None,
        perf_baseline_value=measurement.baseline_value if measurement is not None else None,
        perf_baseline_round=measurement.baseline_round if measurement is not None else None,
        perf_baseline_commit=(
            _text(measurement.baseline_commit) if measurement is not None else None
        ),
        perf_delta_reason=measurement_delta_reason(hypothesis),
        kept=hypothesis.candidate_retained,
        strategy_disposition=hypothesis.strategy.value,
        strategy_reason=hypothesis.strategy_reason,
        active=hypothesis.hypothesis_id == active_id,
    )


@dataclass(frozen=True)
class ExperimentQueryResult:
    """Entries and application metadata returned by one experiment query."""

    entries: list[HypothesisEntry]
    update: ExperimentUpdate


@dataclass(frozen=True)
class ExperimentLoadToken:
    """Projection generation that an authoritative load may replace."""

    generation: int


@dataclass(frozen=True)
class _Delta:
    from_revision: int
    through_revision: int
    changed_ids: frozenset[str]
    removed_ids: frozenset[str]


class ExperimentProjection:
    """Revisioned, bounded cache of the frontend-neutral experiment projection."""

    def __init__(self, *, history_limit: int = 64) -> None:
        """Initialize an empty projection with bounded revision history."""
        self._lock = RLock()
        self._history: deque[_Delta] = deque(maxlen=history_limit)
        self._run_id: str | None = None
        self._source_id: str | None = None
        self._projection_id: str | None = None
        self._revision = 0
        self._entries: dict[str, HypothesisEntry] = {}
        self._hypothesis_indices: dict[str, int] = {}
        self._ready = False
        self._force_reset = False
        self._generation = 0

    def invalidate(self, run_id: str, projection_id: str, revision: int | None) -> None:
        """Record a committed invalidation without reading authoritative state."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            if revision is None:
                self._force_reset = True
            elif revision > self._revision:
                # A state hint normally advances the projection in the event
                # listener. If it was unavailable, require an authoritative
                # reload rather than treating the old cursor as unchanged.
                self._force_reset = True
            self._generation += 1

    def update(
        self,
        run_id: str,
        projection_id: str,
        state: AgentRunState,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Apply one already-committed state without filesystem access."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            if not self._ready or self._force_reset:
                self._replace(state, rotate=self._ready)
            else:
                self._advance(state, changed_keys=changed_keys)
            self._generation += 1

    def replace(
        self,
        run_id: str,
        projection_id: str,
        state: AgentRunState,
    ) -> ExperimentQueryResult:
        """Install an authoritative snapshot and return it as a reset."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            self._replace(state, rotate=self._ready)
            self._generation += 1
            return self._full_result()

    def install_loaded(
        self,
        run_id: str,
        projection_id: str,
        state: AgentRunState,
        token: ExperimentLoadToken,
    ) -> ExperimentQueryResult | None:
        """Install a loaded snapshot only if no newer observation won the race."""
        with self._lock:
            if (
                self._run_id != run_id
                or self._source_id != projection_id
                or self._generation != token.generation
            ):
                return None
            self._replace(state, rotate=self._ready)
            self._generation += 1
            return self._full_result()

    def query(
        self,
        run_id: str,
        projection_id: str,
        cursor: ExperimentCursor | None,
    ) -> ExperimentQueryResult | ExperimentLoadToken:
        """Return a cached result or a token for one authoritative reload."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            if not self._ready or self._force_reset:
                return ExperimentLoadToken(self._generation)
            if (
                cursor is None
                or cursor.run_id != run_id
                or cursor.projection_id != self._projection_id
            ):
                return self._full_result()
            if cursor.revision == self._revision:
                return self._delta_result(cursor.revision, set(), set())
            changes = self._changes_after(cursor.revision)
            if changes is None:
                return self._full_result()
            changed_ids, removed_ids = changes
            return self._delta_result(cursor.revision, changed_ids, removed_ids)

    def _replace(self, state: AgentRunState, *, rotate: bool = False) -> None:
        if rotate:
            self._projection_id = self._new_projection_id()
        entries = build_experiment_log(state)
        self._entries = {entry.hypothesis_id: entry for entry in entries}
        self._hypothesis_indices = {
            hypothesis.hypothesis_id: index for index, hypothesis in enumerate(state.hypotheses)
        }
        self._revision = state.experiment_revision
        self._history.clear()
        self._ready = True
        self._force_reset = False

    def _advance(  # noqa: C901  # Fallback validation keeps cache updates safe.
        self,
        state: AgentRunState,
        *,
        changed_keys: tuple[str, ...] | None,
    ) -> None:
        if state.experiment_revision < self._revision:
            # The persisted cursor regressed under the same run id. This can
            # only be a restored or legacy state, so the old delta chain is no
            # longer a valid base for any client.
            self._replace(state, rotate=True)
            return
        if state.experiment_revision == self._revision:
            # A complete committed snapshot at the current revision can come
            # from restoring legacy state. It starts a new cursor chain so no
            # client can mistake different contents for an unchanged result.
            if changed_keys is None:
                self._replace(state, rotate=True)
            return
        if changed_keys is None:
            previous_ids = set(self._entries)
            current_ids = {hypothesis.hypothesis_id for hypothesis in state.hypotheses}
            removed_ids = previous_ids - current_ids
            changed_ids = {
                hypothesis.hypothesis_id
                for hypothesis in state.hypotheses
                if hypothesis.last_experiment_revision > self._revision
            }
        else:
            removed_ids = set()
            changed_ids = set(changed_keys)
            if len(state.hypotheses) < len(self._hypothesis_indices):
                self._replace(state)
                return
            # Lifecycle transitions append new hypotheses and preserve all
            # existing positions. Index only the appended suffix, so one new
            # hypothesis does not require scanning the prior history.
            for index in range(len(self._hypothesis_indices), len(state.hypotheses)):
                hypothesis = state.hypotheses[index]
                self._hypothesis_indices[hypothesis.hypothesis_id] = index
        # A missing per-entry revision means the state came from a writer that
        # predates deltas. Rebuild rather than returning an incomplete update.
        if not changed_ids and not removed_ids:
            self._replace(state)
            return
        for hypothesis_id in changed_ids:
            index = self._hypothesis_indices.get(hypothesis_id)
            if index is None or index >= len(state.hypotheses):
                self._replace(state)
                return
            hypothesis = state.hypotheses[index]
            if hypothesis.hypothesis_id != hypothesis_id:
                self._replace(state)
                return
            self._entries[hypothesis_id] = build_experiment_entry(
                hypothesis,
                active_id=state.active_hypothesis_id,
            )
        for hypothesis_id in removed_ids:
            self._entries.pop(hypothesis_id, None)
        self._history.append(
            _Delta(
                from_revision=self._revision,
                through_revision=state.experiment_revision,
                changed_ids=frozenset(changed_ids),
                removed_ids=frozenset(removed_ids),
            )
        )
        self._revision = state.experiment_revision

    def _changes_after(self, revision: int) -> tuple[set[str], set[str]] | None:
        cursor = revision
        changed_ids: set[str] = set()
        removed_ids: set[str] = set()
        for delta in self._history:
            if delta.through_revision <= revision:
                continue
            if delta.from_revision != cursor:
                return None
            cursor = delta.through_revision
            # Apply membership changes in revision order. A later upsert must
            # revive an id removed by an earlier transition and vice versa.
            changed_ids.difference_update(delta.removed_ids)
            removed_ids.update(delta.removed_ids)
            removed_ids.difference_update(delta.changed_ids)
            changed_ids.update(delta.changed_ids)
        if cursor != self._revision:
            return None
        return changed_ids, removed_ids

    def _full_result(self) -> ExperimentQueryResult:
        return ExperimentQueryResult(
            entries=self._ordered(self._entries),
            update=ExperimentUpdate(
                run_id=self._run_id or "",
                projection_id=self._projection_id or "",
                through_revision=self._revision,
                reset=True,
            ),
        )

    def _delta_result(
        self,
        from_revision: int,
        changed_ids: set[str],
        removed_ids: set[str],
    ) -> ExperimentQueryResult:
        changed = {
            hypothesis_id: self._entries[hypothesis_id]
            for hypothesis_id in changed_ids
            if hypothesis_id in self._entries
        }
        return ExperimentQueryResult(
            entries=self._ordered(changed),
            update=ExperimentUpdate(
                run_id=self._run_id or "",
                projection_id=self._projection_id or "",
                from_revision=from_revision,
                through_revision=self._revision,
                reset=False,
                removed_hypothesis_ids=sorted(removed_ids),
            ),
        )

    @staticmethod
    def _ordered(entries: dict[str, HypothesisEntry]) -> list[HypothesisEntry]:
        return sorted(entries.values(), key=lambda entry: (entry.first_round, entry.hypothesis_id))

    def _reset(self, run_id: str, projection_id: str) -> None:
        self._run_id = run_id
        self._source_id = projection_id
        self._projection_id = self._new_projection_id()
        self._revision = 0
        self._entries = {}
        self._hypothesis_indices = {}
        self._history.clear()
        self._ready = False
        self._force_reset = False
        self._generation += 1

    def _new_projection_id(self) -> str:
        return f"{self._source_id or 'projection'}:{uuid4().hex}"


def _round(record: RoundRecord) -> HypothesisRound:
    return HypothesisRound(
        round=record.round_number,
        passed=record.passed,
        reviewed=record.reviewed,
        hypothesis_outcome=_outcome(record.hypothesis_outcome),
        judge_verdict=record.judge_verdict,
        perf_metric=record.perf_metric,
        perf_unit=_text(record.perf_unit),
        perf_delta_pct=record.perf_delta_pct,
        commit=_text(record.commit),
        official_evaluation=record.official_evaluation,
        candidate_disposition=_disposition(record.candidate_disposition),
    )


def _outcome(value: str | None) -> HypothesisOutcome | HypothesisResolution | None:
    """Read a stored outcome as one of the two vocabularies that produce it.

    A round record holds the implementer's declared outcome unless the
    framework resolved the hypothesis, in which case it holds the resolution
    instead. Anything else is a legacy or retired value with no meaning for a
    client, so it projects as "not recorded" rather than failing the log.
    """
    if not value:
        return None
    for vocabulary in (HypothesisOutcome, HypothesisResolution):
        member = vocabulary.__members__.get(value.upper())
        if member is not None and member.value == value:
            return member
    return None


def _disposition(value: str | None) -> CandidateDisposition | None:
    """Read a stored disposition, dropping values the framework retired."""
    if not value:
        return None
    member = CandidateDisposition.__members__.get(value.upper())
    return member if member is not None and member.value == value else None


def _judge_verdict(hypothesis: Hypothesis) -> Literal["pass", "fail"] | None:
    value = hypothesis.review.value
    return value if value in ("pass", "fail") else None


def _text(value: str | None) -> str | None:
    return value or None
