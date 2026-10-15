"""One-shot host commands behind an interface, so a test can script what a status tool printed."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_sim.processes import ProcessOutcome, ProcessSpec

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.processes import Processes


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


class ProcessesProbe:
    """Runs the command through a :class:`~vs_sim.processes.Processes`, so the run owns what starts.

    A command that cannot start, outlives the timeout, or writes past the output cap is
    unavailable, as for any probe.
    """

    def __init__(self, processes: Processes) -> None:
        """Start commands with ``processes``."""
        self._processes = processes

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult | None:
        """Run ``argv`` and decode its output; undecodable bytes are replaced."""
        try:
            run = self._processes.run(ProcessSpec(tuple(argv)), timeout=timeout_seconds)
        except OSError:
            return None
        if not isinstance(run, ProcessOutcome):
            return None
        return ProbeResult(
            run.returncode,
            run.stdout.decode(errors="replace"),
            run.stderr.decode(errors="replace"),
        )
