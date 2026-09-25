"""Build one agent round record from final attempt and trusted gate evidence.

# TODO(stack PR 07): remove. Glue re-export: definitions now live in
# ``vibesys.search.hypothesis.record``.
"""

from __future__ import annotations

from vibesys.search.hypothesis.record import (
    CandidateEvidence,
    MeasurementEvidence,
    RecordInput,
    build_round_record,
)

__all__ = [
    "CandidateEvidence",
    "MeasurementEvidence",
    "RecordInput",
    "build_round_record",
]
