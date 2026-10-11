"""The text the issue board needs from the strategy's templates: issue bodies and progress entries."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.core_policy import prompts

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_issue_tracker.api import LogProgress


class IssueQueueTexts:
    """Render bootstrap issue bodies and progress entries from this folder's templates."""

    def __init__(self, variables: Mapping[str, object]) -> None:
        """Bind the run-level template variables (the run facts) the bodies read."""
        self._variables = dict(variables)

    def issue_body(self, template: str) -> str:
        """The description of an issue the strategy filed from a template."""
        return prompts.issue_body(template, **self._variables)

    def progress_entry(self, request: LogProgress) -> str:
        """One human-readable progress entry for a turn's recorded reply."""
        payload = json.loads(request.payload_json) if request.payload_json else {}
        return prompts.progress_entry(
            iteration=request.iteration,
            step=request.step,
            issue_id=request.issue_id,
            payload=payload if isinstance(payload, dict) else {},
        )
