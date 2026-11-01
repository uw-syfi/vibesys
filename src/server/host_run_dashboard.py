"""What a run's tile draws, as the host helper's ``runs.list`` carries it (``with_dashboard``).

The digest (``server.run_digest``) folds a run's journal into the figures of a list row; the
dashboard is the same pass's per-idea chart and the reason a run needs its user. These are the
contracts only: Pydantic and the standard library, so ``server.host_api`` imports them into
``RunDigest`` and ``RunSummary`` without the fold.

Public surface: ``RunClosing``, ``IdeaVerdict``, ``IdeaPoint``, ``kept_landings``, ``BestReading``, ``RunDashboard``, ``RunAttention`` and the
limits ``DASHBOARD_MAX_POINTS``, ``REPEATED_FAILURE_AFTER`` and ``STALLED_AFTER_S``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NonNegativeInt

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime


class RunClosing(StrEnum):
    """The journal's last word on a run: the terminal event it ends with, if it has one."""

    FINISHED = "finished"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class IdeaVerdict(StrEnum):
    """What a run's idea came to, as the run page's chart marks it."""

    KEPT = "kept"
    """It passed and beat every passed idea before it and the input: it sits on the step line."""
    NOT_BETTER = "not_better"
    """It passed and did not beat the best so far."""
    FAILED = "failed"
    """Every attempt of the idea failed."""
    RUNNING = "running"
    """An attempt of the idea is still going: its verdict is not decided."""


class IdeaPoint(BaseModel):
    """One idea of a run on the performance chart: its result against the input and its verdict.

    ``ratio`` is worded like ``RunDigest.adopted_ratio`` (an improvement over the input, for a
    ``min`` objective the input's value over the result's); ``None`` when the idea has no
    measurement against the input (it failed before one, or still works).

    ``at`` is when the idea's result landed, by the journal's event clock: the time of the gate event
    that last wrote the reading the idea is plotted by, else of the idea's last event; ``None`` when
    the journal's event has no readable time. It is the one definition of when a ``kept`` idea became
    the best (the usage rollup counts new bests by it); the span clock orders the chart but is not a
    time of day.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ratio: float | None
    verdict: IdeaVerdict
    at: AwareDatetime | None = None


def kept_landings(points: Iterable[IdeaPoint]) -> tuple[datetime, ...]:
    """When each ``kept`` idea of ``points`` landed, in the order of the points.

    The one definition of a new best's time, shared by every count of them (the usage rollup and
    the digest), so two counts of one run cannot disagree. A kept point whose journal event had no
    readable time has none and is not counted.
    """
    return tuple(
        point.at for point in points if point.verdict is IdeaVerdict.KEPT and point.at is not None
    )


DASHBOARD_MAX_POINTS: Final = 64
"""The most ideas one ``RunDashboard`` carries, so a ``runs.list`` of ``RUN_DIGEST_MAX_RUNS`` runs
stays far under ``HELPER_LINE_MAX_BYTES``."""


class BestReading(BaseModel):
    """The measured value of the run's best idea so far, in the run's own metric.

    It is what the run page shows as the best idea's value when no ratio to the input can be worded
    (the input was not measured): the reading of the last ``kept`` idea of the chart, with the
    objective's ``metric`` id and ``unit`` as the journal's ``run_started`` names them (either may be
    ``None``: a run names no unit, or its journal predates the overview).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float
    metric: str | None
    unit: str | None


class RunDashboard(BaseModel):
    """What a run's tile draws, folded from the journal in the digest's pass.

    ``points`` are the run page's chart (``performanceFold``): the ideas in the order their results
    landed (the idea's measurement time, else when its last attempt ended or began; ties in the
    order the ideas began), each with its verdict. A run with more than ``DASHBOARD_MAX_POINTS``
    ideas keeps the newest points of the verdicts that matter to the line: the oldest ideas that
    are not ``kept`` go first, then the oldest ``kept`` ones, and ``points_omitted`` counts the
    ideas left out (the ideas count is ``len(points) + points_omitted``). ``failed_streak`` counts
    the ideas at the end of the order that failed, with none passing since (a running idea is
    skipped), over every idea and not only the ones kept. ``last_event_at`` is the journal's last
    event, the only time fact a stalled run is judged from (``RunAttention``). ``best`` is the
    reading of the best idea so far (``BestReading``); absent when no idea was kept with a reading.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    points: Annotated[tuple[IdeaPoint, ...], Field(max_length=DASHBOARD_MAX_POINTS)]
    points_omitted: NonNegativeInt
    failed_streak: NonNegativeInt
    last_event_at: AwareDatetime | None
    best: Annotated[BestReading | None, Field(exclude_if=lambda each: each is None)] = None


class RunAttention(StrEnum):
    """Why a run needs its user, judged from its journal, its server and the time now."""

    NONE = "none"
    FAILED = "failed"
    """The run ended in failure: its terminal event, or its server's phase, says so."""
    REPEATED_FAILURE = "repeated_failure"
    """The run still works and its last ``REPEATED_FAILURE_AFTER`` ideas all failed."""
    STALLED = "stalled"
    """A server drives the run and its journal has not moved for ``STALLED_AFTER_S``."""


REPEATED_FAILURE_AFTER: Final = 3
"""The ideas in a row that fail before a working run needs its user."""

STALLED_AFTER_S: Final = 30 * 60
"""The seconds a run's live server may leave its journal silent before the run is stalled: a
benchmark or a long agent turn keeps writing output, so half an hour of silence is not work."""
