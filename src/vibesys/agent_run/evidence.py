"""Pure evidence-summarization helpers over agent-run round records.

# TODO(stack PR 07): remove. Glue re-export: definitions now live in
# ``vibesys.search.hypothesis.transitions``, under their (now public) names.
# This module re-exports them under the old, underscore-prefixed names that
# callers still import.
"""

from __future__ import annotations

from vibesys.search.hypothesis.transitions import (
    FAILED_HYPOTHESIS_OUTCOMES as _FAILED_HYPOTHESIS_OUTCOMES,
)
from vibesys.search.hypothesis.transitions import CarryOver, _format_metric_row
from vibesys.search.hypothesis.transitions import detect_plateau as _detect_plateau
from vibesys.search.hypothesis.transitions import (
    pareto_archive_conflict as _pareto_archive_conflict,
)
from vibesys.search.hypothesis.transitions import (
    pareto_archive_dominators as _pareto_archive_dominators,
)
from vibesys.search.hypothesis.transitions import pareto_archive_summary as _pareto_archive_summary
from vibesys.search.hypothesis.transitions import (
    pareto_frontier_records as _pareto_frontier_records,
)
from vibesys.search.hypothesis.transitions import (
    provisional_candidate_retained as _provisional_candidate_retained,
)
from vibesys.search.hypothesis.transitions import (
    provisional_candidates_since_official as _provisional_candidates_since_official,
)
from vibesys.search.hypothesis.transitions import (
    record_candidate_metrics as _record_candidate_metrics,
)
from vibesys.search.hypothesis.transitions import select_final_candidate as _select_final_candidate
from vibesys.search.hypothesis.transitions import (
    terminal_workspace_notice as _terminal_workspace_notice,
)
from vibesys.search.hypothesis.transitions import (
    trusted_candidate_records as _trusted_candidate_records,
)

__all__ = [
    "_FAILED_HYPOTHESIS_OUTCOMES",
    "CarryOver",
    "_detect_plateau",
    "_format_metric_row",
    "_pareto_archive_conflict",
    "_pareto_archive_dominators",
    "_pareto_archive_summary",
    "_pareto_frontier_records",
    "_provisional_candidate_retained",
    "_provisional_candidates_since_official",
    "_record_candidate_metrics",
    "_select_final_candidate",
    "_terminal_workspace_notice",
    "_trusted_candidate_records",
]
