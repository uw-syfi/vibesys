"""Neutral run identity resolution shared by the runner and runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.orchestration.request import RunRequestLike


def resolved_run_id(request: RunRequestLike) -> str:
    """Return the run id the loop should use: the resume target, or `exp_name`."""
    if request.resume is not None:
        return request.resume.run_id
    if request.exp_name is None:
        message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    return request.exp_name
