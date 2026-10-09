"""How a hypothesis-search run ends, derived from the work it did.

A terminal status is derived from the durable records, never assumed: a run that
measured nothing did not complete. There are three endings, and success always
means the operator has a trusted result to use:

- ``adopted``: a trusted, retained candidate was selected (success);
- ``no improvement``: no candidate was selected, but the input was measured and
  trusted, so the operator keeps it (success);
- ``no trusted result``: nothing was measured and trusted, so there is nothing to
  keep (failure).

``ending_of`` is the one decision table. Strategies whose evidence is a list of
:class:`RoundRecord` use :func:`derive_ending`; a strategy that keeps its own
input measurement passes its two facts to ``ending_of`` directly.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, assert_never

from vibesys.hypothesis.transitions import select_final_candidate, trusted_perf_provenance

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys.hypothesis.history import RoundRecord
    from vibesys.metrics import MetricSpace


class RunEnding(StrEnum):
    """How a finished run ends; the leading words of a result reason."""

    ADOPTED = "adopted"
    NO_IMPROVEMENT = "no improvement"
    NO_TRUSTED_RESULT = "no trusted result"

    @property
    def succeeded(self) -> bool:
        """Whether the operator has a trusted result: a winner, or the measured input."""
        match self:
            case RunEnding.ADOPTED | RunEnding.NO_IMPROVEMENT:
                return True
            case RunEnding.NO_TRUSTED_RESULT:
                return False
            case _ as unreachable:
                assert_never(unreachable)


def ending_of(*, adopted: bool, trusted_reading: bool) -> RunEnding:
    """Decide the ending from whether a winner was selected and whether anything was trusted.

    ``trusted_reading`` is true when the run holds a framework-owned measurement it can
    stand behind: the measured input, or a measured candidate that did not win.
    """
    if adopted:
        return RunEnding.ADOPTED
    if trusted_reading:
        return RunEnding.NO_IMPROVEMENT
    return RunEnding.NO_TRUSTED_RESULT


def has_trusted_reading(record: RoundRecord) -> bool:
    """Whether a round holds a fresh measurement the framework took itself."""
    return (
        record.passed
        and record.official_evaluation
        and not record.profile_skipped
        and trusted_perf_provenance(record.perf_provenance)
        and (record.perf_metric is not None or bool(record.metrics))
    )


def derive_ending(records: Sequence[RoundRecord], space: MetricSpace) -> RunEnding:
    """Derive the ending of a hypothesis-search run from its completed round records."""
    return ending_of(
        adopted=select_final_candidate(records, space) is not None,
        trusted_reading=any(has_trusted_reading(record) for record in records),
    )


__all__ = ["RunEnding", "derive_ending", "ending_of", "has_trusted_reading"]
