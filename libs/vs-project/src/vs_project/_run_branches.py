"""Run-owned Git names: mutable working history and checkout-independent publications."""

from __future__ import annotations

ADOPTION_BRANCH_PREFIX = "vibesys-adoptions/"

RUN_BRANCH_PREFIX = "vibesys-runs/"
"""The branch hierarchy that holds every run's branch."""


def run_branch(run_id: str) -> str:
    """The branch that holds run ``run_id``'s history."""
    return f"{RUN_BRANCH_PREFIX}{run_id}"


def run_of_branch(branch: str) -> str | None:
    """The run id a run branch is named after, or ``None`` for any other branch."""
    for prefix in (RUN_BRANCH_PREFIX, ADOPTION_BRANCH_PREFIX):
        if branch.startswith(prefix):
            return branch.removeprefix(prefix) or None
    return None


def adoption_branch(run_id: str) -> str:
    """The run-owned publication branch, separate from its mutable checkout branch."""
    return f"{ADOPTION_BRANCH_PREFIX}{run_id}"


def adoption_revision_ref(run_id: str, revision: str) -> str:
    """The immutable publication receipt for one source revision of a run."""
    return f"refs/vibesys/{run_id}/adoptions/{revision}"
