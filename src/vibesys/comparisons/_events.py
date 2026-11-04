"""A run's comparison readings as the event the run journals."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import ComparisonReadingData, ComparisonsReadData, ComparisonStatus
from vs_runtime.api.comparisons import (
    ComparisonFailed,
    ComparisonMeasured,
    ComparisonNotConfigured,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from vibesys.comparisons.api import ComparisonReport


def _row(report: ComparisonReport) -> ComparisonReadingData:
    reading = report.reading
    if isinstance(reading, ComparisonMeasured):
        return ComparisonReadingData(
            name=report.name,
            label=report.label,
            status=ComparisonStatus.MEASURED,
            metric=reading.metric,
            unit=reading.unit,
            direction=reading.direction,
            value=reading.value,
            spread=reading.spread,
            samples=len(reading.samples),
            hosts=reading.hosts,
        )
    if isinstance(reading, ComparisonFailed):
        return ComparisonReadingData(
            name=report.name,
            label=report.label,
            status=ComparisonStatus.FAILED,
            reason=reading.reason,
        )
    if isinstance(reading, ComparisonNotConfigured):
        return ComparisonReadingData(
            name=report.name,
            label=report.label,
            status=ComparisonStatus.NOT_CONFIGURED,
            reason=reading.reason,
        )
    return ComparisonReadingData(
        name=report.name, label=report.label, status=ComparisonStatus.NOT_MEASURED
    )


def comparisons_read_data(reports: Iterable[ComparisonReport]) -> ComparisonsReadData:
    """The event payload for *reports*, one row each, in the task's order."""
    return ComparisonsReadData(comparisons=tuple(_row(report) for report in reports))
