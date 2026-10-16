"""Where a run's history lives in Git: the branch named after the run."""

from __future__ import annotations

RUN_BRANCH_PREFIX = "vibesys-runs/"
"""The branch hierarchy that holds every run's branch."""


def run_branch(run_id: str) -> str:
    """The branch that holds run ``run_id``'s history."""
    return f"{RUN_BRANCH_PREFIX}{run_id}"


def run_of_branch(branch: str) -> str | None:
    """The run id a run branch is named after, or ``None`` for any other branch."""
    if not branch.startswith(RUN_BRANCH_PREFIX):
        return None
    return branch.removeprefix(RUN_BRANCH_PREFIX) or None
