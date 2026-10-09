"""Where a trusted benchmark gate may write its result file.

A benchmark gate is told one output path (``<output argument> <path>``). Two
shapes are allowed, and nothing else, because the path is attacker-controlled
text on its way to a trusted process:

* a **workspace** result, ``.vibesys-benchmark-<id>.json`` in the working
  directory, which both sides of a shared filesystem see;
* a **framework** result, ``/tmp/vibesys-framework-benchmark-<hex>.json``, the
  path the framework's own trusted benchmark uses. It names a file in the
  *caller's* ``/tmp``; a caller that does not share that ``/tmp`` with the
  gate's host has the file relayed to it.

Both the Slurm gate wrapper and the host command broker classify with this one
function, so they cannot disagree about what is allowed.
"""

from __future__ import annotations

import re
from enum import StrEnum

#: The ``<output argument> <path>`` argument count of a benchmark gate.
OUTPUT_ARGUMENT_COUNT = 2

_WORKSPACE_OUTPUT = re.compile(r"\.vibesys-benchmark-[A-Za-z0-9_-]{1,64}\.json")
_FRAMEWORK_OUTPUT = re.compile(
    r"/tmp/vibesys-framework-benchmark-[0-9a-f]{1,64}\.json"  # noqa: S108  # lint-waiver: LW-352320 [S108]; the fixed framework benchmark transport path, not a temp file.
)


class BenchmarkOutputKind(StrEnum):
    """The allowed shapes of a benchmark gate's result path."""

    WORKSPACE = "workspace"
    FRAMEWORK = "framework"


def classify_benchmark_output(path: str) -> BenchmarkOutputKind | None:
    """Return which allowed shape *path* has, or ``None`` when it has neither.

    Matches the whole string, so a separator, a ``..`` component, or a trailing
    newline never passes.
    """
    if _WORKSPACE_OUTPUT.fullmatch(path):
        return BenchmarkOutputKind.WORKSPACE
    if _FRAMEWORK_OUTPUT.fullmatch(path):
        return BenchmarkOutputKind.FRAMEWORK
    return None
