"""The differential-dataflow crash-recovery gate injects a crash at every point it is asked to.

A real engine process: the gate reads its stdout through a pipe, and what it does with lines that
arrive together is a property of the pipe, so it is tested over a real one.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from tests.examples.example_task import task_root

if TYPE_CHECKING:
    from types import ModuleType

_PROJECT = Path(__file__).resolve().parents[2] / "examples" / "database" / "differential-dataflow"

# Prints three lines in one write, then stays alive the first time it runs in a directory: what
# an engine does when its output arrives in a burst. A second run in the same directory (the
# gate's restart) prints them and exits.
_BURST_ENGINE = (
    "import os, sys, time\n"
    "first = not os.path.exists('started')\n"
    "open('started', 'w').close()\n"
    "sys.stdout.write('1\\n2\\n3\\n')\n"
    "sys.stdout.flush()\n"
    "if first:\n"
    "    time.sleep(60)\n"
)


def _load_gate() -> ModuleType:
    path = task_root(_PROJECT) / "accuracy_checker" / "crash_recovery_gate.py"
    spec = importlib.util.spec_from_file_location(
        "differential_dataflow_crash_recovery_burst", path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_crash_injection_counts_lines_that_arrive_together(tmp_path: Path) -> None:
    """Regression: a buffered readline kept the second line of a burst where select could not see
    it, so the gate waited on an empty pipe, reported no stdout, and skipped the crash point."""
    crash_gate = _load_gate()

    crashed, ok, message = crash_gate._crash_then_restart(  # noqa: SLF001  # lint-waiver: LW-008509 [SLF001]; exercise the crash point without building the external engine or running unrelated CLI gates.
        sys.executable, ["-c", _BURST_ENGINE], tmp_path, 2
    )

    assert crashed is True, message
    assert ok is True, message
