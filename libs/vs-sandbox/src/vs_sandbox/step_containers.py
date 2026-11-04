"""The Docker candidate containers one run holds for its steps, listed and removed one at a time.

A candidate container opened for a step carries :data:`~vs_sandbox.step_marks.STEP_TAG_LABEL`
next to the run's label. Listing reads the labels from the daemon, so the containers of one step
are found by its tag alone and no other step's container is touched, which a run-wide sweep
(``vs_agent.orphans``) cannot promise inside a live run.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.docker_sandbox import RUN_ID_LABEL
from vs_sandbox.docker_states import (
    INITIAL_CONTAINER_LIFE,
    RemovalReading,
    observe_removal,
    released,
)
from vs_sandbox.step_marks import STEP_TAG_LABEL
from vs_sim.api import DISCARD, ProcessTimeoutError

if TYPE_CHECKING:
    from vs_sandbox.docker_cli import DockerCli

_DOCKER_TIMEOUT_S = 60.0


@dataclass(frozen=True, slots=True)
class _Reply:
    """What a ``docker`` command printed and how it exited."""

    returncode: int
    stdout: str
    stderr: str


class StepContainerError(RuntimeError):
    """The daemon could not list the step containers or remove one."""


@dataclass(frozen=True, slots=True)
class StepContainer:
    """A container held for a step."""

    id: str
    tag: str
    """The step mark it was opened under."""


class StepContainers(Protocol):
    """The containers a run holds for its steps."""

    def listed(self) -> tuple[StepContainer, ...]:
        """Every container, running or not, that carries a step label.

        Raises :class:`StepContainerError` when the daemon cannot be asked.
        """
        ...

    def remove(self, container: StepContainer) -> None:
        """Force-remove *container*; one already gone, or already being removed, is released.

        Raises :class:`StepContainerError` when the daemon cannot say the container is gone.
        """
        ...


class DockerStepContainers:
    """The step containers of one run, through the ``docker`` client."""

    def __init__(self, docker: DockerCli, run_id: str) -> None:
        """List and remove the containers labelled with *run_id* through *docker*."""
        self._docker = docker
        self._run_id = run_id

    def listed(self) -> tuple[StepContainer, ...]:
        """The run's labelled containers, read with their step labels."""
        argv = [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={RUN_ID_LABEL}={self._run_id}",
            "--filter",
            f"label={STEP_TAG_LABEL}",
            "--format",
            f'{{{{.ID}}}} {{{{.Label "{STEP_TAG_LABEL}"}}}}',
        ]
        result = self._run(argv, "list the step containers")
        rows = (line.split() for line in result.stdout.splitlines() if line.strip())
        return tuple(StepContainer(id=row[0], tag=row[1]) for row in rows if len(row) == 2)  # noqa: PLR2004  # lint-waiver: LW-914303 [PLR2004]; a row is an id and a tag.

    def remove(self, container: StepContainer) -> None:
        """Remove through ``docker rm -f``, read by the one lifecycle translator."""
        what = f"remove container {container.id}"
        result = self._run(["docker", "rm", "-f", container.id], what, check=False)
        life = observe_removal(
            INITIAL_CONTAINER_LIFE, RemovalReading(result.returncode, result.stderr)
        )
        if not released(life):
            message = (
                f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
            )
            raise StepContainerError(message)

    def _run(self, argv: list[str], what: str, *, check: bool = True) -> _Reply:
        try:
            result = self._docker.run(argv, timeout_seconds=_DOCKER_TIMEOUT_S, output=DISCARD)
        except (OSError, ProcessTimeoutError) as error:
            message = f"could not {what}: {error}"
            raise StepContainerError(message) from error
        if check and result.returncode != 0:
            message = (
                f"could not {what}: docker exited {result.returncode}: {result.stderr.strip()}"
            )
            raise StepContainerError(message)
        return _Reply(result.returncode, result.stdout, result.stderr)
