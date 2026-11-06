"""Running loadgen and the generator: their command lines (``docs/spec.md``) behind small interfaces.

``ProcessLoadgen`` runs the binary, which starts the generator command that
follows ``--`` and reads the request stream from its standard output;
``ProcessCommands`` runs the generator's ``check``. Tests substitute fakes
that run the same command lines and write the same files.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys_evaluator._private import core
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: Names the loadgen binary; consulted after the wheel's bundled one (``core.find_binary``).
LOADGEN_ENV = core.ENV_VARS["loadgen"]
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


class Commands(Protocol):
    """Runs a command (``argv[0]`` and its arguments) and waits for it."""

    def run(self, argv: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """Run ``argv``; a run past ``timeout_secs`` is stopped and reported as exit 124."""
        ...


@dataclass(frozen=True, kw_only=True)
class PhaseFiles:
    """The files loadgen writes in one run."""

    results: Path
    summary: Path


def phase_args(
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
    generator: Sequence[str],
) -> list[str]:
    """The loadgen arguments of one phase; ``generator`` is the command that writes its stream."""
    common = [
        "--secs",
        repr(float(secs)),
        "--warmup",
        repr(float(warmup_secs)),
        "--results",
        str(files.results),
        "--summary",
        str(files.summary),
        "--timeout",
        repr(STEP_TIMEOUT_SECS),
    ]
    stream = ["--", *generator]
    if model == pb.LoadModel.CLOSED:
        return ["closed", "--target", target, "--clients", str(clients), *common, *stream]
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
        *stream,
    ]


def _run(argv: Sequence[str], timeout_secs: float, name: str) -> LoadgenExit:
    """Run ``argv``; stderr is kept (its tail), stdout is passed through."""
    try:
        done = subprocess.run(
            list(argv), stderr=subprocess.PIPE, text=True, timeout=timeout_secs, check=False
        )
    except subprocess.TimeoutExpired as error:
        stderr = (
            error.stderr.decode(errors="replace")
            if isinstance(error.stderr, bytes)
            else (error.stderr or "")
        )
        return LoadgenExit(
            code=124, stderr=f"{name} ran past {timeout_secs:.0f}s\n{stderr[-_STDERR_TAIL:]}"
        )
    except OSError as error:
        return LoadgenExit(code=127, stderr=f"cannot run {name} at {argv[0]!r}: {error.strerror}")
    return LoadgenExit(code=done.returncode, stderr=done.stderr[-_STDERR_TAIL:])


class ProcessLoadgen:
    """The loadgen binary, run as a child process."""

    def __init__(self, path: str | None = None) -> None:
        """Use ``path``, else the wheel's bundled loadgen, ``$VIBESYS_EVALUATOR_LOADGEN`` or ``PATH``."""
        self._missing = ""
        if path is None:
            try:
                path = core.find_binary("loadgen")
            except core.CoreNotFoundError as error:
                self._missing = str(error)
                path = "loadgen"
        self._path = path

    def run(self, args: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """Run it; stderr (its own and the generator's) is kept (its tail), stdout is passed through."""
        if self._missing:
            return LoadgenExit(code=127, stderr=self._missing)
        return _run([self._path, *args], timeout_secs, "loadgen")


class ProcessCommands:
    """Commands run as child processes."""

    def run(self, argv: Sequence[str], *, timeout_secs: float) -> LoadgenExit:
        """Run ``argv``; stderr is kept (its tail), stdout is passed through."""
        return _run(argv, timeout_secs, argv[0])
