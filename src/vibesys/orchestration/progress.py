"""Per-round progress entries that prompts may point agents at.

A plugin's progress log holds one Markdown file per round under its progress
directory. Prompts tell agents that some text "is in the progress entry"; that
pointer is only true if a writer appended the text before the prompt was sent.

This module makes the pointer a value. :meth:`ProgressLog.append` is the only
way to obtain a :class:`ProgressEntry`, and it returns one only after the
section is on disk. A prompt context that points at an entry takes a
``ProgressEntry | None`` field, so the prompt can mention the entry exactly
when the entry was written. ``tests/architecture/test_progress_pointers.py``
requires every template sentence that names a progress entry to be guarded by
such a field.

Sections are rendered from templates: :meth:`ProgressLog.append` rejects text
that did not come out of a :class:`~vs_prompts.api.TemplateRenderer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vs_prompts.api import RenderedPrompt

if TYPE_CHECKING:
    from pathlib import Path

# Private: only ProgressLog.append mints a ProgressEntry.
_WRITTEN = object()


@dataclass(frozen=True, slots=True)
class ProgressEntry:
    """Proof that a section was appended to a round's progress file.

    ``location`` is the workspace-relative path of that file. Only
    :meth:`ProgressLog.append` can construct one; any other caller gets
    ``TypeError``.
    """

    location: str
    token: object = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        """Reject construction outside :meth:`ProgressLog.append`."""
        if self.token is not _WRITTEN:
            message = "ProgressEntry is produced only by ProgressLog.append"
            raise TypeError(message)


@dataclass(frozen=True, slots=True)
class CarriedEntries:
    """The entries holding a new round's carried notices; ``None`` when not carried."""

    regression: ProgressEntry | None
    exhaustion: ProgressEntry | None


@dataclass(frozen=True, slots=True)
class ProgressLog:
    """Append-only round files under ``directory`` inside ``workspace``."""

    workspace: Path
    directory: Path

    def append(self, round_number: int, section: RenderedPrompt) -> ProgressEntry:
        """Append a rendered section to round ``round_number`` and return its entry.

        Raises ``TypeError`` when ``section`` was not rendered from a template.
        """
        if not isinstance(section, RenderedPrompt):
            message = "progress sections must be rendered from a template"
            raise TypeError(message)
        path = self.directory / f"round-{round_number:04d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(section)
        return ProgressEntry(path.relative_to(self.workspace).as_posix(), _WRITTEN)


__all__ = ["CarriedEntries", "ProgressEntry", "ProgressLog"]
