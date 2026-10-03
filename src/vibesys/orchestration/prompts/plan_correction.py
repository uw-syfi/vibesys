"""Correction brief sent when a designer's plan fails state validation.

The wording lives in ``shared/plan_correction_prompt.j2``; both the multi and
single plugins render it here so neither imports the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.orchestration.prompts.renderer import render_template

if TYPE_CHECKING:
    from collections.abc import Collection


def render_plan_correction(
    *,
    error: str,
    hypothesis_id: str,
    updated_hypothesis_ids: Collection[str],
    require_unseen_id: bool,
) -> str:
    """Render the one-shot correction brief for a rejected plan.

    ``updated_hypothesis_ids`` names the prior hypotheses the rejected plan
    updated; ``require_unseen_id`` adds the instruction to pick an identifier
    that never appeared in the run.
    """
    return render_template(
        "shared/plan_correction_prompt.j2",
        error=error,
        hypothesis_id=hypothesis_id,
        updated_hypothesis_ids=sorted(set(updated_hypothesis_ids)),
        require_unseen_id=require_unseen_id,
    )
