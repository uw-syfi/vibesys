"""Issue-queue agent prompts and board views, rendered from the templates beside this module.

Each template reads only the fields of its typed `PromptContext` plus the run-level variables
the plan supplies (`facts`, `agent_evaluation`). Replies are typed, so no template mentions
the issue board's own tools: an agent files an issue by naming it in its reply.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_prompts.api import RenderedPrompt, TemplateRenderer

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vs_issue_tracker.api import Issue, IssueStatus

RENDERER = TemplateRenderer(Path(__file__).parent)
"""The renderer over this folder, for the prompts that are not rendered through an operation."""


def issue_markdown(issue: Issue) -> RenderedPrompt:
    """Render the Markdown view of one issue and its history."""
    return RENDERER.render_template("board/issue.j2", issue=issue)


def issue_index(
    groups: Sequence[tuple[IssueStatus, Sequence[Issue]]], filenames: Mapping[int, str]
) -> RenderedPrompt:
    """Render the board index from non-empty status groups in display order."""
    return RENDERER.render_template("board/index.j2", groups=groups, filenames=filenames)


def progress_entry(
    *, iteration: int, step: str, issue_id: int | None, payload: Mapping[str, object]
) -> RenderedPrompt:
    """Render one paid turn's record for the run progress log."""
    return RENDERER.render_template(
        "board/progress.j2", iteration=iteration, step=step, issue_id=issue_id, payload=payload
    )


def issue_body(template: str, **variables: object) -> RenderedPrompt:
    """Render the description of an issue the strategy files from a template."""
    return RENDERER.render_template(f"{template}.j2", **variables)


__all__ = ["RENDERER", "issue_body", "issue_index", "issue_markdown", "progress_entry"]
