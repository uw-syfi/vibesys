"""Gate reporting helpers."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_correctness.models import VerificationReport


def write_report(report: VerificationReport, path: str | Path) -> None:
    """Atomically persist a report containing cases, observations, and replay data."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(report.model_dump_json(indent=2))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary_path.replace(destination)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def gate_exit_code(report: VerificationReport) -> int:
    """Return zero only for a nonempty all-PASS report."""
    return 0 if report.passed else 1
