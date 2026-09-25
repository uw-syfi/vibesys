"""The progress board's framework log: gate verdicts, block writing.

# TODO(stack PR 07): this is an interim, role-independent slice of the real
# module. The full module also renders role-dependent blocks (pre-round
# decision, profiler summary, orchestrator plan, hypothesis continuation,
# implementer, judge, single-agent round, exhaustion note, ...), which need
# `vibesys.roles` reply types (PR 06) and stay owned by the strategy layer
# until PR 07 migrates `loops/multi` and `loops/single` onto `ctx.agents.turn`.
# PR 07 replaces this file with the full REF version; nothing here should
# survive past that point unchanged.

This is job (2) of the progress board (see
:mod:`vibesys.orchestration.memory` and :mod:`vibesys.orchestration.artifacts`
for the other two jobs): the framework's own narration of what it decided and
observed each round, distinct from role handoffs (job 1, ``artifacts.py``)
and declared agent memory (job 3, ``memory.py``).

Every ``render_*`` function here is pure: it takes the same typed data a gate
already produced and returns the exact Markdown block the board used to
write immediately. :func:`write` is the one place that turns a rendered
block into a file mutation, reusing the replace-by-heading merge the board
has always used so a resumed round replaces its own stable heading instead
of duplicating it.

This module lives under ``vibesys.orchestration`` (its own logic, not the
dissolved ``agent_run``) so the host itself -- ``orchestration.state``'s
``ctx.state.commit`` and ``orchestration.gates``'s ``ctx.gates.run`` -- can
call :func:`write` directly.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

#: Every block this module renders starts with this heading shape; the round
#: number is recovered from it rather than threaded separately through the
#: buffer, so the buffer stays a plain ``list[str]``.
_HEADING_ROUND = re.compile(r"^## Round (\d+) — ")

_PROGRESS_HEADER = "# Progress\n\n"


def render_framework_accuracy_gate(  # noqa: D103  # tracked: #288
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    output: str,
) -> str:
    verdict = "pass" if passed else "fail"
    return (
        f"## Round {round_number} — Framework accuracy gate (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n\n"
        f"### Output\n{output or '(no output)'}\n"
    )


def render_framework_benchmark(  # noqa: D103, PLR0913  # tracked: #288
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    metric_name: str | None,
    metric_value: float | None,
    output: str,
) -> str:
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
        raise ValueError(f"framework log block missing a round heading: {heading!r}")  # noqa: TRY003
    return int(match.group(1))


def write(progress_path: Path, block: str) -> None:
    """Write one framework-owned progress section idempotently.

    A run can be resumed after a process exits between recording a phase
    result and finishing the round. The resumed phase has the same stable
    Markdown heading (round, role, and attempt), so replace that section
    instead of appending a duplicate. Distinct attempts retain distinct
    headings and therefore remain separate audit entries.

    Self-contained (does not import ``vibesys.orchestration.memory``, which
    itself would depend on this module for its own writes): ensures the
    progress document exists the same way
    ``memory.ensure_progress_file`` does, in the ``.md`` or directory
    layout the caller already resolved.
    """
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
        # A framework section owns its H3 children, but not a neighboring H2.
        # Operators and recovery tooling may append evidence under their own H2
        # between an interrupted phase and resume. Preserve that evidence when
        # replacing the stable framework heading.
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1

    if not replaced:
        with document.open("a", encoding="utf-8") as fh:
            fh.write(normalized_block)
        return

    # Replacement rewrites an existing audit section. Keep the prior file
    # intact if the process exits during the write, then atomically publish
    # the completed document.
    replacement = document.with_name(f".{document.name}.tmp")
    replacement.write_text("".join(output), encoding="utf-8")
    replacement.replace(document)
