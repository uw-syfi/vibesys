"""One-shot host commands behind an interface, so a test can script what a status tool printed."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class ProbeResult:
    """How a probed command ended: its exit status and what it wrote."""

    returncode: int
    stdout: str
    stderr: str = ""


class CommandProbe(Protocol):
    """Runs a short, non-interactive command to completion and reports what it printed.

    For status and capability queries (``nvidia-smi``, ``rocm-smi``, a dry run of a
    confinement tool) whose caller treats "could not run" and "took too long" as the
    same answer: not available. Long-lived or streaming processes use
    :class:`~vs_sim.processes.ProcessLauncher` instead.
    """

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult | None:
        """Run ``argv`` with no input; ``None`` when the program cannot start or outlives the timeout."""
        ...


class SubprocessProbe:
    """Runs the command as a real operating-system process, blocking the calling thread."""

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult | None:
        """Run ``argv`` and capture its text output; undecodable bytes are replaced."""
        try:
            completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-731020 [S603]; the caller supplies an argv vector it assembled itself, and no shell is involved.
                list(argv),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return ProbeResult(completed.returncode, completed.stdout, completed.stderr)
