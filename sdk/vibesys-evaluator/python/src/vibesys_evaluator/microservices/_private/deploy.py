"""The deploy layer's interface: bring up the system under test and run phases in it.

The orchestrator (``main``) depends only on this module; each deploy kind
(``microservices.k8s``, and the local runner the tests use) implements it. A
``Deployer`` brings the system up and hands out a ``PhaseRunner`` for as long
as the system is up; the runner runs one phase of the task's own script next
to the system (in Kubernetes, a pod of the task image in the namespace) and
copies the phase's files back.

What crosses this interface is files and command lines, never Python objects:
the phase input and output are ``phase.proto`` messages, and the commands are
built by ``TaskCommand``, so the far side can be another process, pod or
machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vibesys_evaluator._private.sample import SampleError

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

#: The file a phase's work directory holds when the phase command starts (a
#: ``PhaseInput``, binpb), written there by the runner.
PHASE_INPUT_FILE = "input.binpb"
#: The file the phase command leaves in its work directory (a ``PhaseOutput``,
#: binpb). Its presence and a zero exit status mean the phase ran.
PHASE_OUTPUT_FILE = "output.binpb"
#: The file a runner writes into the output directory: the phase command's
#: standard output and standard error, combined.
PHASE_LOG_FILE = "phase.log"


class DeployError(SampleError):
    """The system under test could not be brought up or torn down.

    The message is what the sample's error record says: name the step and
    what to fix, with the tail of the relevant log.
    """


class PhaseRunError(SampleError):
    """A phase command did not finish: it exited non-zero, timed out or could not start.

    The message names the phase and carries the tail of its log.
    """


@dataclass(frozen=True, kw_only=True)
class TaskCommand:
    """The command lines that run the task's own script in another process of the task image.

    Attributes:
        python: The interpreter, as a path that is valid where the command runs.
        script: The task script (``bench.py``), as a path valid where the command runs.
    """

    python: str
    script: str

    def phase(self, *, work_dir: str, target: str) -> tuple[str, ...]:
        """The command of one phase.

        Args:
            work_dir: A directory holding ``PHASE_INPUT_FILE``; the command
                writes ``PHASE_OUTPUT_FILE`` and the phase's other files there.
            target: The base URL of the system's gateway, as reachable from
                where the command runs.

        Returns:
            The argument vector.
        """
        return (self.python, self.script, "phase", "--work", work_dir, "--target", target)

    def seed(self, *, dataset_digest: str) -> tuple[str, ...]:
        """The command that writes the initial data into the system (``App.seed``).

        Args:
            dataset_digest: The orchestrator's dataset digest; the command
                regenerates the dataset and refuses to seed a different one.

        Returns:
            The argument vector.
        """
        return (self.python, self.script, "seed", "--dataset-digest", dataset_digest)


@dataclass(frozen=True, kw_only=True)
class DeployContext:
    """What a deployer is given for one sample.

    Attributes:
        project_root: The candidate's checkout (``--project-root``).
        artifacts: A directory for the deploy's logs and summaries. It exists;
            it is kept with the job when vibesys gives an artifacts directory.
        task: How to run the task's script for the seed and the phases.
        dataset_digest: The digest of the sample's dataset, for ``task.seed``.
        log: Writes one progress line for the person watching the job.
    """

    project_root: Path
    artifacts: Path
    task: TaskCommand
    dataset_digest: str
    log: Callable[[str], None]


@dataclass(frozen=True, kw_only=True)
class PhaseJob:
    """One phase to run.

    Attributes:
        name: Unique within the sample (``phase-0``, ``phase-1``, ...): a
            suffix for the pod's name and the artifacts directory.
        phase_input: The ``PhaseInput`` message, binpb. The runner writes it
            to ``PHASE_INPUT_FILE`` in the work directory before the command
            starts.
        timeout_secs: The most the phase may take, from starting the command
            to its files being copied back. A runner raises ``PhaseRunError``
            past it.
    """

    name: str
    phase_input: bytes
    timeout_secs: float


class PhaseRunner(Protocol):
    """Runs phases next to the system under test while it is up."""

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        """Run ``task.phase(work_dir=W, target=gateway)`` with the job's input in W.

        Copy every file the command left in W into ``out_dir`` (an existing,
        empty directory) and write the command's combined output to
        ``PHASE_LOG_FILE`` there, also when it fails.

        Args:
            job: The phase.
            out_dir: Where its files go.

        Raises:
            PhaseRunError: The command exited non-zero, timed out, or could
                not be started or reached.
        """
        ...


class Deployer(Protocol):
    """Brings the system under test up for one sample, and down again."""

    def deploy(self, context: DeployContext) -> AbstractContextManager[PhaseRunner]:
        """Bring the system up: build, data tier, seed (``context.task.seed``), the rest, readiness.

        Leaving the context tears everything down, on every path (return,
        exception, ``SystemExit`` from a signal).

        Args:
            context: The sample's inputs.

        Returns:
            A context manager whose value runs phases against the system.

        Raises:
            DeployError: A step failed.
        """
        ...
