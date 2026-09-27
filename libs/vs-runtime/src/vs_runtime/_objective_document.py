"""Validated filesystem materialization for one effective run objective."""

from __future__ import annotations

from typing import TYPE_CHECKING

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
