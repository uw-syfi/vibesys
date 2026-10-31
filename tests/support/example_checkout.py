"""A source checkout with a one-example catalog, for tests of what a host says of its examples."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

CATALOG_EXAMPLE = "structures/stack"
"""The one example ``example_checkout`` catalogues, by the name the catalog lists it under."""


def example_task(project: Path, name: str) -> None:
    """Give ``project`` the task ``name``, with the files a task needs to be discovered."""
    task = project / ".vibesys" / "tasks" / name
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("version = 1\n", encoding="utf-8")


def example_checkout(root: Path) -> Path:
    """A checkout at ``root`` whose catalog holds ``CATALOG_EXAMPLE`` with one task, ``spsc``."""
    example = root / "examples" / "structures" / "stack"
    example.mkdir(parents=True)
    (example / "src.txt").write_text("candidate\n", encoding="utf-8")
    example_task(example, "spsc")
    (root / "examples" / "registry.toml").write_text(
        'version = 1\n\n[[example]]\npath = "examples/structures/stack"\n', encoding="utf-8"
    )
    return root
