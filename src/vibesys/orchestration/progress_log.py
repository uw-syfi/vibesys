"""Framework-owned evaluation records in the human-readable progress log."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_HEADING_ROUND = re.compile(r"^## Round (\d+) — ")
_PROGRESS_HEADER = "# Progress\n\n"


def render_framework_accuracy_gate(
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    output: str,
) -> str:
    """Render one framework accuracy-gate result."""
    verdict = "pass" if passed else "fail"
    return (
        f"## Round {round_number} — Framework accuracy gate (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n\n"
        f"### Output\n{output or '(no output)'}\n"
    )


def render_framework_benchmark(  # noqa: PLR0913  # LW-040106 [PLR0913]; these are independent observed benchmark fields, and bundling them would hide ownership.
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    metric_name: str | None,
    metric_value: float | None,
    output: str,
) -> str:
    """Render framework benchmark metrics and diagnostics."""
    verdict = "pass" if passed else "fail"
    metric_line = (
        f"- **{metric_name}**: {metric_value}\n"
        if metric_name is not None and metric_value is not None
        else ""
    )
    return (
        f"## Round {round_number} — Framework benchmark (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n"
        f"{metric_line}\n"
        f"### Output\n{output or '(no output)'}\n"
    )


def _round_number(block: str) -> int:
    heading = block.splitlines()[0]
    match = _HEADING_ROUND.match(heading)
    if match is None:
        message = f"framework log block missing a round heading: {heading!r}"
        raise ValueError(message)
    return int(match.group(1))


def write(progress_path: Path, block: str) -> None:
    """Write one framework-owned progress section idempotently."""
    round_number = _round_number(block)
    if progress_path.suffix == ".md":
        if not progress_path.exists():
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(_PROGRESS_HEADER)
    else:
        progress_path.mkdir(parents=True, exist_ok=True)
    document = (
        progress_path
        if progress_path.suffix == ".md"
        else progress_path / f"round-{round_number:04d}.md"
    )
    if not document.exists() and document != progress_path:
        document.write_text(f"# Round {round_number}\n\n")

    heading = block.splitlines()[0]
    normalized_block = block.rstrip("\n") + "\n\n"
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    output: list[str] = []
    replaced = False
    index = 0
    while index < len(lines):
        if lines[index].rstrip("\r\n") != heading:
            output.append(lines[index])
            index += 1
            continue

        if not replaced:
            output.append(normalized_block)
            replaced = True
        index += 1
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1

    if not replaced:
        with document.open("a", encoding="utf-8") as stream:
            stream.write(normalized_block)
        return

    replacement = document.with_name(f".{document.name}.tmp")
    replacement.write_text("".join(output), encoding="utf-8")
    replacement.replace(document)


__all__ = ["render_framework_accuracy_gate", "render_framework_benchmark", "write"]
