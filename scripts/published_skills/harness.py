"""The per-harness reference files a published skill carries, and the contract they share.

A skill that needs a harness capability holds ``references/harness/<harness>.md`` for each
supported harness (``skills/README.md``). Every file of one skill answers the same capabilities,
under the same headings, so adding a harness never changes what a skill asks of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scripts.published_skills.findings import Finding

if TYPE_CHECKING:
    from pathlib import Path

HARNESS_FOLDER = "references/harness"
HARNESS_HEADINGS = (
    "Dispatch a subagent with a fresh context",
    "Run a long command as a tracked background task",
    "Locate a skill's own scripts",
    "Schedule periodic work",
    "Wait for work without polling",
    "Packaging",
)
"""The capabilities a harness file may answer, in the order ``skills/README.md`` lists them."""


def check_harness_files(skill_folder: Path, shown_root: str) -> list[Finding]:
    """Findings for a skill whose harness files break the shared heading contract.

    ``skill_folder`` is the skill's directory and ``shown_root`` its path as findings show it.
    A skill without a harness folder has nothing to check.
    """
    files = sorted((skill_folder / HARNESS_FOLDER).glob("*.md"))
    findings: list[Finding] = []
    headings: dict[str, tuple[str, ...]] = {}
    for file in files:
        shown = f"{shown_root}/{HARNESS_FOLDER}/{file.name}"
        found = _headings(file.read_text(encoding="utf-8"))
        headings[file.name] = tuple(found)
        findings.extend(
            Finding(shown, f"heading `{heading}` is not one of the harness capabilities")
            for heading in found
            if heading not in HARNESS_HEADINGS
        )
        known = [heading for heading in found if heading in HARNESS_HEADINGS]
        if known != sorted(set(known), key=HARNESS_HEADINGS.index):
            findings.append(
                Finding(shown, "headings must be unique and in the order of skills/README.md")
            )
    if len({frozenset(value) for value in headings.values()}) > 1:
        names = ", ".join(sorted(headings))
        findings.append(
            Finding(
                f"{shown_root}/{HARNESS_FOLDER}",
                f"{names} must answer the same capabilities; a harness without one says what to "
                "do instead",
            )
        )
    return findings


def _headings(text: str) -> list[str]:
    """The text of every ``## `` heading outside fenced code blocks, in order."""
    found: list[str] = []
    fenced = False
    for line in text.splitlines():
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            found.append(line[3:].strip())
    return found
