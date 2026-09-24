"""Reshape a run's `RunView` into experiment-log protocol entries.

`vibesys.api`'s `RunView`/`HypothesisView`/`HypothesisRoundView` already
derive every fact this module publishes -- copying authoritative state,
never grouping rounds, selecting a baseline, or inferring a resolution (see
`vibesys.api._readmodel`). This module only reshapes those boundary DTOs into
the server's own wire types (`HypothesisEntry`/`HypothesisRound`) and caches
the reshaped result, revisioned so a client can fetch only what changed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING, Literal
from uuid import uuid4

from server.api.protocol import ExperimentCursor, ExperimentUpdate, HypothesisEntry, HypothesisRound

if TYPE_CHECKING:
    from vibesys.api import HypothesisRoundView, HypothesisView, RunView

_StrategyDisposition = Literal["available", "parked", "abandoned"]


def build_experiment_log(run_view: RunView) -> list[HypothesisEntry]:
    """Return the complete hypothesis history in stable start-round order."""
    return sorted(
        (_to_hypothesis_entry(hypothesis) for hypothesis in run_view.hypotheses),
        key=lambda entry: (entry.first_round, entry.hypothesis_id),
    )


def _strategy_disposition(value: str) -> _StrategyDisposition:
    """Narrow `HypothesisView.strategy_disposition` back to its closed set.

    `vibesys.api.HypothesisView` widens this to plain `str` at the API
    boundary rather than leaking core's own `HypothesisStrategy` enum (see
    its docstring); the only producer of the value
    (`vibesys.api._readmodel`, from `Hypothesis.strategy.value`) writes
    exactly one of these three strings, so an unrecognized value here means
    the boundary DTO's contract was violated upstream.
    """
    if value == "available":
        return "available"
    if value == "parked":
        return "parked"
    if value == "abandoned":
        return "abandoned"
    message = f"unknown hypothesis strategy disposition: {value!r}"
    raise ValueError(message)


def _to_hypothesis_entry(hypothesis: HypothesisView) -> HypothesisEntry:
    """Reshape one `HypothesisView` into its wire DTO, field-for-field."""
    return HypothesisEntry(
        hypothesis_id=hypothesis.hypothesis_id,
        title=hypothesis.title,
        claim=hypothesis.claim,
        action=hypothesis.action,
        first_round=hypothesis.first_round,
        last_round=hypothesis.last_round,
        rounds=[_to_hypothesis_round(record) for record in hypothesis.rounds],
        resolved_outcome=hypothesis.resolved_outcome,
        judge_verdict=hypothesis.judge_verdict,
        perf_metric=hypothesis.perf_metric,
        perf_unit=hypothesis.perf_unit,
        perf_delta_pct=hypothesis.perf_delta_pct,
        perf_metric_name=hypothesis.perf_metric_name,
        perf_direction=hypothesis.perf_direction,
        perf_baseline_value=hypothesis.perf_baseline_value,
        perf_baseline_round=hypothesis.perf_baseline_round,
        perf_baseline_commit=hypothesis.perf_baseline_commit,
        perf_delta_reason=hypothesis.perf_delta_reason,
        kept=hypothesis.kept,
        strategy_disposition=_strategy_disposition(hypothesis.strategy_disposition),
        strategy_reason=hypothesis.strategy_reason,
        active=hypothesis.active,
    )


def _to_hypothesis_round(record: HypothesisRoundView) -> HypothesisRound:
    """Reshape one `HypothesisRoundView` into its wire DTO, field-for-field."""
    return HypothesisRound(
        round=record.round_number,
        passed=record.passed,
        reviewed=record.reviewed,
        hypothesis_outcome=record.hypothesis_outcome,
        judge_verdict=record.judge_verdict,
        perf_metric=record.perf_metric,
        perf_unit=record.perf_unit,
        perf_delta_pct=record.perf_delta_pct,
        commit=record.commit,
        official_evaluation=record.official_evaluation,
        candidate_disposition=record.candidate_disposition,
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
        run_view: RunView,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Apply one already-committed view without filesystem access."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            if not self._ready or self._force_reset:
                self._replace(run_view, rotate=self._ready)
            else:
                self._advance(run_view, changed_keys=changed_keys)
            self._generation += 1

    def replace(
        self,
        run_id: str,
        projection_id: str,
        run_view: RunView,
    ) -> ExperimentQueryResult:
        """Install an authoritative snapshot and return it as a reset."""
        with self._lock:
            if self._source_id != projection_id:
                self._reset(run_id, projection_id)
            self._replace(run_view, rotate=self._ready)
            self._generation += 1
            return self._full_result()

    def install_loaded(
        self,
        run_id: str,
        projection_id: str,
        run_view: RunView,
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
            self._replace(run_view, rotate=self._ready)
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

    def _replace(self, run_view: RunView, *, rotate: bool = False) -> None:
        if rotate:
            self._projection_id = self._new_projection_id()
        entries = build_experiment_log(run_view)
        self._entries = {entry.hypothesis_id: entry for entry in entries}
        self._hypothesis_indices = {
            hypothesis.hypothesis_id: index for index, hypothesis in enumerate(run_view.hypotheses)
        }
        self._revision = run_view.experiment_revision
        self._history.clear()
        self._ready = True
        self._force_reset = False

    def _advance(  # Fallback validation keeps cache updates safe.
        self,
        run_view: RunView,
        *,
        changed_keys: tuple[str, ...] | None,
    ) -> None:
        if run_view.experiment_revision < self._revision:
            # The persisted cursor regressed under the same run id. This can
            # only be a restored or legacy state, so the old delta chain is no
            # longer a valid base for any client.
            self._replace(run_view, rotate=True)
            return
        if run_view.experiment_revision == self._revision:
            # A complete committed snapshot at the current revision can come
            # from restoring legacy state. It starts a new cursor chain so no
            # client can mistake different contents for an unchanged result.
            if changed_keys is None:
                self._replace(run_view, rotate=True)
            return
        if changed_keys is None:
            previous_ids = set(self._entries)
            current_ids = {hypothesis.hypothesis_id for hypothesis in run_view.hypotheses}
            removed_ids = previous_ids - current_ids
            changed_ids = {
                hypothesis.hypothesis_id
                for hypothesis in run_view.hypotheses
                if hypothesis.last_experiment_revision > self._revision
            }
        else:
            removed_ids = set()
            changed_ids = set(changed_keys)
            if len(run_view.hypotheses) < len(self._hypothesis_indices):
                self._replace(run_view)
                return
            # Lifecycle transitions append new hypotheses and preserve all
            # existing positions. Index only the appended suffix, so one new
            # hypothesis does not require scanning the prior history.
            for index in range(len(self._hypothesis_indices), len(run_view.hypotheses)):
                hypothesis = run_view.hypotheses[index]
                self._hypothesis_indices[hypothesis.hypothesis_id] = index
        # A missing per-entry revision means the state came from a writer that
        # predates deltas. Rebuild rather than returning an incomplete update.
        if not changed_ids and not removed_ids:
            self._replace(run_view)
            return
        for hypothesis_id in changed_ids:
            index = self._hypothesis_indices.get(hypothesis_id)
            if index is None or index >= len(run_view.hypotheses):
                self._replace(run_view)
                return
            hypothesis = run_view.hypotheses[index]
            if hypothesis.hypothesis_id != hypothesis_id:
                self._replace(run_view)
                return
            self._entries[hypothesis_id] = _to_hypothesis_entry(hypothesis)
        for hypothesis_id in removed_ids:
            self._entries.pop(hypothesis_id, None)
        self._history.append(
            _Delta(
                from_revision=self._revision,
                through_revision=run_view.experiment_revision,
                changed_ids=frozenset(changed_ids),
                removed_ids=frozenset(removed_ids),
            )
        )
        self._revision = run_view.experiment_revision

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
