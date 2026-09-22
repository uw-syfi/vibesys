"""Vocabulary for ordering two benchmark results.

This is the one persisted-state concept the loops share about measurement:
how a candidate reading relates to the reading it is compared against. It
lives here, rather than in ``vibesys``, so a round record can store the
comparison the framework made without this library depending on loop code.

Unlike ``hypothesis_outcome`` and ``candidate_disposition``, which
``RoundRecord`` persists as plain strings so a legacy record with a retired
value still loads (see ``vs_loop_state.agent``), the ordering of two numbers
has no such compatibility concern, so it is a real enum.
"""

from __future__ import annotations

from enum import StrEnum


class MetricComparison(StrEnum):
    """How one measurement relates to the measurement it is compared against.

    ``WITHIN_NOISE`` and ``INCOMPARABLE`` are deliberately distinct.
    ``WITHIN_NOISE`` means both readings exist on the same axis and their
    difference does not exceed the declared measurement tolerance, so the
    comparison ran and found no result. ``INCOMPARABLE`` means the comparison
    could not run at all: a reading is missing, or the axis has no known
    direction. Consumers that collapse them still have to say so explicitly.
    """

    BETTER = "better"
    WORSE = "worse"
    WITHIN_NOISE = "within_noise"
    INCOMPARABLE = "incomparable"
