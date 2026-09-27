"""Product projection derived after runtime commits opaque plugin state."""

# The observer translates a durable runtime transition through its temporary host.
# lint-waiver: LW-920434 [SLF001]; this thin composition adapter is removed with RunContext.
# ruff: noqa: SLF001

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from vibesys.events import CoreEventType, EventStatus, ExperimentsChangedData, RoundFinishedData
from vibesys.orchestration import progress_log

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.events import CoreEventData
    from vibesys.orchestration._host import HostResources
    from vibesys.orchestration.view import RoundSummary, RunView


class _EventSink(Protocol):
    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> object: ...


class _StateCommitObserver:
    """Publish semantic product projections after runtime durability succeeds."""

    def __init__(self, host: HostResources, namespace: str) -> None:
        self._host = host
        self._namespace = namespace

    def committed(self, previous: BaseModel | None, current: BaseModel) -> None:
        self._host._resources.publish_committed_state(self._namespace, current)
        _emit_commit_events(self._host.events, self._project(previous), self._project(current))
        progress = self._host.progress
        if progress.path is not None:
            for block in progress.drain():
                progress_log.write(progress.path, block)

    def _project(self, value: BaseModel | None) -> RunView | None:
        projector = self._host._projector
        if projector is None or value is None:
            return None
        return projector.project_committed(
            self._namespace,
            value,
            run_id=self._host._resources.run_id,
        )


def _round_entries(view: RunView | None) -> dict[int, RoundSummary]:
    if view is None:
        return {}
    return {round_summary.number: round_summary for round_summary in view.rounds}


def _emit_commit_events(events: _EventSink, before: RunView | None, after: RunView | None) -> None:
    """Emit semantic changes newly made observable by one durable commit."""
    if after is None:
        return
    before_rounds = _round_entries(before)
    after_rounds = _round_entries(after)
    new_round_numbers = sorted(number for number in after_rounds if number not in before_rounds)
    for number in new_round_numbers:
        _emit_round_finished(events, after_rounds[number])
    if before is None:
        return
    before_revision = before.experiment_revision
    after_revision = after.experiment_revision
    if after_revision is not None and after_revision != before_revision:
        reason = "round_persisted" if new_round_numbers else "active_hypothesis_changed"
        events.emit(
            CoreEventType.EXPERIMENTS_CHANGED,
            data=ExperimentsChangedData(reason=reason, revision=after_revision),
        )


def _emit_round_finished(events: _EventSink, round_summary: RoundSummary) -> None:
    status = EventStatus.FAILED if round_summary.status == "failed" else EventStatus.COMPLETED
    events.emit(
        CoreEventType.ROUND_FINISHED,
        status=status,
        round_label=f"round-{round_summary.number}",
        data=RoundFinishedData(
            attempts=round_summary.attempts,
            judge_verdict=round_summary.judge_verdict or "skipped",
            perf_metric=round_summary.perf_metric,
            perf_unit=round_summary.perf_unit,
            profile_skipped=round_summary.profile_skipped,
        ),
    )
