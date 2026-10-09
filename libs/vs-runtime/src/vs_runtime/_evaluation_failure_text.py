"""The failure text a submitting agent reads when its evaluation fails.

Each function renders a template that owns all wording; callers pass facts only.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_prompts.api import TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_evaluation.api import EvidenceKind
    from vs_prompts.api import RenderedPrompt

_RENDERER = TemplateRenderer(Path(__file__).with_name("prompts"))


def render_rejected_evidence(
    rejected: Sequence[tuple[str | None, EvidenceKind]],
) -> RenderedPrompt:
    """One line per rejected ``(semantic summary, kind)``: the summary, else ``<kind> failed``."""
    return _RENDERER.render_template("rejected_evidence.j2", rejected=rejected)


def render_evaluation_failure(
    record_failure: str | None, stage_failure: str | None
) -> RenderedPrompt:
    """A failed evaluation's own message, else its first stage failure, else a generic one."""
    return _RENDERER.render_template(
        "evaluation_failure.j2", record_failure=record_failure, stage_failure=stage_failure
    )


def render_stage_failure(
    rejected: Sequence[tuple[str | None, EvidenceKind]], observed_failure: str | None
) -> RenderedPrompt:
    """The failure of an evaluation whose failed stage skipped the rest.

    One line per failed check (its summary, else ``<kind> check failed``), else
    the executor's own failure, else a generic stage failure.
    """
    return _RENDERER.render_template(
        "stage_failure.j2", rejected=rejected, observed_failure=observed_failure
    )
