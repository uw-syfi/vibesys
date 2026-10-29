"""Validated filesystem materialization for one effective run objective."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from pathlib import Path


def materialize_objective_document(
    objective: str,
    *,
    workspace: Path,
    authored_document: Path | None,
    destination: Path,
) -> Path:
    """Return a verified authored objective or persist *objective* at *destination*.

    An authored document must resolve inside *workspace*, be a regular file,
    and contain the exact effective objective. Without one, the caller-selected
    destination receives the objective verbatim.
    """
    if authored_document is not None:
        path = authored_document.resolve()
        try:
            path.relative_to(workspace.resolve())
        except ValueError as exc:
            message = f"effective objective must be inside the project workspace: {path}"
            raise ValueError(message) from exc
        if not path.is_file() or path.read_text() != objective:
            message = f"effective objective does not match its committed document: {path}"
            raise ValueError(message)
        return path

    destination.write_text(objective)
    return destination


def outside_objective(
    objective: str | None, log_dir: Path
) -> tuple[Path | None, tuple[HostResource, ...]]:
    """Give an agent on the host a copy of the effective objective beside the run's logs.

    The committed objective sits in the run's record below ``.vibesys/state/runs``, which
    agent confinement hides, so the agent reads this copy instead, as a container agent
    reads its mount. The committed document stays the authority: the copy carries exactly
    the effective objective. Returns the copy and the read-only grant its confinement needs.
    """
    if objective is None:
        return None, ()
    log_dir.mkdir(parents=True, exist_ok=True)
    copy = materialize_objective_document(
        objective,
        workspace=log_dir,
        authored_document=None,
        destination=log_dir / "effective-objective.md",
    )
    return copy, (HostResource(copy, HostResourceAccess.READ_ONLY),)
