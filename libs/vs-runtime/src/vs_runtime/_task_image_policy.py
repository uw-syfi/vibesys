"""Which image a run environment's agent container builds from.

A task may ship ``.vibesys/tasks/<task>/Dockerfile``. Every run environment
whose agent runs in a local Docker container (Docker, and the editor of Modal,
SkyPilot and Slurm) builds that Dockerfile on the backend's default image
(passed as ``BASE_IMAGE``) and layers the agent Features on top. The image ID
the build returns is the environment's ``image`` option, so the run record
pins it and resume rebuilds and compares instead of drifting silently.

:func:`editor_image_plan` is the one decision for every environment; the entry
point performs the build it asks for and checks the result with
:meth:`EditorImagePlan.resolve`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

#: Environments whose agent container is a local Docker editor built through
#: ``agent_image``: each one honors a task Dockerfile the same way.
CONTAINER_EDITOR_ENVIRONMENTS: frozenset[str] = frozenset(
    {"docker", "modal", "skypilot", "slurm", "slurm-gpu"}
)
#: Environments with no agent container: a task Dockerfile has nothing to build.
_NO_CONTAINER_ENVIRONMENTS: frozenset[str] = frozenset({"host"})


class TaskImageError(ValueError):
    """A task Dockerfile cannot be honored as the run was asked to run."""

    @classmethod
    def no_container(cls, environment: str, dockerfile: Path) -> TaskImageError:
        """Describe an environment that runs the agent without a container."""
        return cls(
            f"the task Dockerfile {dockerfile} cannot be honored: the {environment} run "
            f"environment runs the agent on the host, with no container to build it into. "
            f"Remove the Dockerfile or run on a backend Docker can reach."
        )

    @classmethod
    def operator_image(cls, dockerfile: Path, image: str) -> TaskImageError:
        """Describe an explicit image paired with a task Dockerfile."""
        return cls(
            f"task Dockerfile {dockerfile} cannot be combined with --docker-image ({image}): "
            f"the task image is built from the Dockerfile on the backend's default image"
        )

    @classmethod
    def recorded_without_task_image(cls, environment: str, dockerfile: Path) -> TaskImageError:
        """Describe a resumed run recorded without the task image it now declares."""
        return cls(
            f"cannot resume: this {environment} run was recorded without a task image, but "
            f"the task now declares {dockerfile}. Resuming in a different image would change "
            f"the candidate's environment mid-run; start a new run, or remove the Dockerfile."
        )

    @classmethod
    def changed_on_resume(cls, dockerfile: Path, recorded: str, built: str) -> TaskImageError:
        """Describe a task image that no longer reproduces the recorded one."""
        return cls(
            f"cannot resume: the task image built from {dockerfile} is {built}, but the run "
            f"was recorded with {recorded}. The Dockerfile or its build context changed since "
            f"the run started; restore them, or start a new run."
        )


@dataclass(frozen=True)
class EditorImagePlan:
    """What a launch builds for its agent container, decided before any build.

    ``task_dockerfile`` is the Dockerfile to build on ``base_image`` (the
    backend's default, passed as ``BASE_IMAGE``), or ``None`` to use ``image``
    as given. When the plan builds, ``image`` is the image ID a resumed run
    recorded, which the build must reproduce, or ``None`` on a fresh run.
    """

    image: str | None
    task_dockerfile: Path | None = None
    base_image: str | None = None

    def resolve(self, built: str | None) -> str | None:
        """Return the image the environment runs, given what the build produced.

        ``built`` is the task image ID, or ``None`` when no build ran (as during
        validation). A resumed run refuses a task image different from the
        recorded one.
        """
        if self.task_dockerfile is None or built is None:
            return self.image
        if self.image is not None and built != self.image:
            raise TaskImageError.changed_on_resume(self.task_dockerfile, self.image, built)
        return built


def editor_image_plan(
    environment: str,
    *,
    task_dockerfile: Path | None,
    image: str | None,
    base_image: str | None,
    launch: Literal["fresh", "resume"],
) -> EditorImagePlan:
    """Decide which image *environment*'s agent container builds from.

    ``image`` is the operator's ``--docker-image`` on a fresh launch, or the
    image the run record holds on resume. ``base_image`` is the backend's
    default image, the task Dockerfile's ``BASE_IMAGE``.

    Raises :class:`TaskImageError` when the task Dockerfile cannot be honored:
    an environment with no agent container, an explicit image beside it, or a
    resumed run recorded without one. ``ValueError`` names an unknown
    environment. Nothing is ever silently ignored.
    """
    if environment not in CONTAINER_EDITOR_ENVIRONMENTS | _NO_CONTAINER_ENVIRONMENTS:
        message = f"unknown run environment: {environment!r}"
        raise ValueError(message)
    if task_dockerfile is None:
        return EditorImagePlan(image=image)
    if environment in _NO_CONTAINER_ENVIRONMENTS:
        raise TaskImageError.no_container(environment, task_dockerfile)
    if launch == "fresh" and image is not None:
        raise TaskImageError.operator_image(task_dockerfile, image)
    if launch == "resume" and image is None:
        raise TaskImageError.recorded_without_task_image(environment, task_dockerfile)
    return EditorImagePlan(image=image, task_dockerfile=task_dockerfile, base_image=base_image)
