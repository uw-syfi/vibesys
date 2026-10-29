"""The projects a scripted agent knows how to change, and how it recognizes one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.scripted._recipes import QUEUE_RECIPES, Recipe

if TYPE_CHECKING:
    from collections.abc import Callable


type Reader = Callable[[str], str | None]
"""Reads a file of the checkout by its path relative to the checkout's root; ``None`` if absent."""


@dataclass(frozen=True, slots=True)
class Workload:
    """A project whose source a scripted implementer edits with a catalog of recipes."""

    name: str
    source: str
    """The file the recipes edit, relative to the project root."""
    manifest: str
    """A file at the root that names the project, relative to the project root."""
    manifest_text: str
    """Text the manifest holds when the project is this workload."""
    recipes: tuple[Recipe, ...]

    def recognizes(self, read: Reader) -> bool:
        """Whether the checkout ``read`` reads is this workload."""
        manifest = read(self.manifest)
        return (
            manifest is not None
            and self.manifest_text in manifest
            and read(self.source) is not None
        )


QUEUE_RS = Workload(
    name="queue-rs",
    source="src/lib.rs",
    manifest="Cargo.toml",
    manifest_text="queue-candidate",
    recipes=QUEUE_RECIPES,
)
"""The bounded queue of `examples/data-structures/repositories/queue-rs` (cheap, CPU-only)."""

WORKLOADS = (QUEUE_RS,)
"""Every workload the scripted agents know."""


def recognized(read: Reader, workloads: tuple[Workload, ...]) -> Workload | None:
    """The workload the checkout ``read`` reads is, or ``None`` when no workload claims it."""
    return next((workload for workload in workloads if workload.recognizes(read)), None)
