"""Running loadgen: its command line (``docs/spec.md``) behind a small interface.

``ProcessLoadgen`` runs the binary; tests substitute a fake that reads the
same requests file and writes the same results and summary files.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Names the loadgen binary; ``loadgen`` on ``PATH`` when unset.
LOADGEN_ENV = "VIBESYS_EVALUATOR_LOADGEN"
#: loadgen's own per-step timeout (its default): bounds how long a phase runs past its window.
STEP_TIMEOUT_SECS = 10.0
_STDERR_TAIL = 2000


@dataclass(frozen=True, kw_only=True)
class LoadgenExit:
    """How a loadgen run ended."""

    code: int
    stderr: str


class Loadgen(Protocol):
    """Runs loadgen with a command line and waits for it."""

    def run(self, args: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """Run ``loadgen ARGS``; a run past ``timeout_secs`` is stopped and reported as exit 124."""
        ...


@dataclass(frozen=True, kw_only=True)
class PhaseFiles:
    """The files of one loadgen run."""

    requests: Path
    results: Path
    summary: Path


def phase_args(  # noqa: PLR0913 - the phase's load, passed through to the command line once
    *,
    model: pb.LoadModel,
    target: str,
    files: PhaseFiles,
    secs: float,
    warmup_secs: float,
    clients: int = 0,
    rate: float = 0.0,
    arrivals: pb.Arrivals = pb.Arrivals.UNSPECIFIED,
    arrivals_seed: int = 0,
    min_sustained: float = 0.0,
) -> list[str]:
    """The loadgen arguments of one phase."""
    common = [
        "--secs",
        repr(float(secs)),
        "--warmup",
        repr(float(warmup_secs)),
        "--requests",
        str(files.requests),
        "--results",
        str(files.results),
        "--summary",
        str(files.summary),
        "--timeout",
        repr(STEP_TIMEOUT_SECS),
    ]
    if model == pb.LoadModel.CLOSED:
        return ["closed", "--target", target, "--clients", str(clients), *common]
    mode = "poisson" if arrivals == pb.Arrivals.POISSON else "uniform"
    return [
        "open",
        "--target",
        target,
        "--rate",
        repr(float(rate)),
        "--arrivals",
        mode,
        "--seed",
        str(arrivals_seed),
        "--min-sustained",
        repr(float(min_sustained)),
        *common,
    ]


class ProcessLoadgen:
    """The loadgen binary, run as a child process."""

    def __init__(self, path: str | None = None) -> None:
        """Use ``path``, else ``$VIBESYS_EVALUATOR_LOADGEN``, else ``loadgen`` on ``PATH``."""
        self._path = path or os.environ.get(LOADGEN_ENV) or shutil.which("loadgen") or "loadgen"

    def run(self, args: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """Run it; stderr is kept (its tail), stdout is passed through."""
        try:
            done = subprocess.run(  # noqa: S603 - the package's own binary, arguments built above
                [self._path, *args],
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_secs,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            stderr = (
                error.stderr.decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else (error.stderr or "")
            )
            return LoadgenExit(
                code=124, stderr=f"loadgen ran past {timeout_secs:.0f}s\n{stderr[-_STDERR_TAIL:]}"
            )
        except OSError as error:
            return LoadgenExit(
                code=127, stderr=f"cannot run loadgen at {self._path!r}: {error.strerror}"
            )
        return LoadgenExit(code=done.returncode, stderr=done.stderr[-_STDERR_TAIL:])
