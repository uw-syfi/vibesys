"""Which image a run environment's agent container builds from.

A task may ship ``.vibesys/tasks/<task>/Dockerfile``. Every run environment
whose agent runs in a local Docker container (``uses_docker``: Docker, and the
agent container of Modal, SkyPilot and Slurm) builds that Dockerfile on the
backend's default image (passed as ``BASE_IMAGE``) and layers the agent
Features on top.

The run records the task image's *reference*: a local tag keyed to the task's
location and to a digest of every build input. On resume:

- the recorded image still in the local daemon is reused without a build;
- otherwise, unchanged inputs rebuild the same reference (the image ID may
  differ when Docker's build cache is gone), with a notice;
- changed inputs are refused.

Runs recorded before references (a bare image ID) are reused when the ID is
still local, else rebuilt as they were built then (without ``BASE_IMAGE``)
and refused unless the rebuild reproduces the recorded ID.

:func:`editor_image_plan` is the one pure decision for every environment. The
entry point gathers :class:`TaskImageFacts` (it reads files and asks Docker),
performs the build the plan asks for, and checks it with
:meth:`EditorImagePlan.resolve`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from vs_runtime._run_environment import (
    DockerEnvironment,
    HostEnvironment,
    ModalEnvironment,
    SkyPilotEnvironment,
)
from vs_runtime._slurm_environment import SlurmEnvironment

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime._run_environment import RunEnvironmentSpec

_ENVIRONMENT_CLASSES: dict[
    str, type[DockerEnvironment | HostEnvironment | ModalEnvironment | SlurmEnvironment]
] = {
    "docker": DockerEnvironment,
    "host": HostEnvironment,
    "modal": ModalEnvironment,
    "skypilot": SkyPilotEnvironment,
    "slurm": SlurmEnvironment,
    "slurm-gpu": SlurmEnvironment,
}


def runs_agent_in_container(environment: str) -> bool:
    """Return whether *environment* runs its agent in a local Docker container.

    Read from the environment class's ``uses_docker``, without constructing it.
    ``ValueError`` names an unknown environment.
    """
    try:
        environment_class = _ENVIRONMENT_CLASSES[environment]
    except KeyError:
        message = f"unknown run environment: {environment!r}"
        raise ValueError(message) from None
    return bool(environment_class.uses_docker)


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
    def not_built(cls, environment: str, dockerfile: Path) -> TaskImageError:
        """Describe a run environment selection that was not built from the task image."""
        return cls(
            f"the task declares {dockerfile}, but the {environment} run environment was "
            f"selected without building it; build the selection with the task image "
            f"(the CLI does) instead of running without it"
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
    def inputs_changed(cls, dockerfile: Path, recorded: str, current: str) -> TaskImageError:
        """Describe task image inputs that differ from the recorded ones."""
        return cls(
            f"cannot resume: the task image inputs changed since the run started (recorded "
            f"{recorded}, now {current}). The Dockerfile, its build context, the task's "
            f"location, or the backend's base image differ from {dockerfile} as recorded; "
            f"restore them, or start a new run."
        )

    @classmethod
    def legacy_image_changed(cls, dockerfile: Path, recorded: str, built: str) -> TaskImageError:
        """Describe a pre-reference run whose image is gone and did not rebuild identically."""
        return cls(
            f"cannot resume: the run was recorded with task image {recorded}, which is no "
            f"longer in the local Docker daemon, and rebuilding {dockerfile} produced {built}. "
            f"The run predates input-keyed task images, so whether its inputs changed or only "
            f"Docker's build cache is gone cannot be told; start a new run."
        )


@dataclass(frozen=True)
class TaskImageFacts:
    """What the entry point read before planning: files and the local daemon.

    ``reference`` is the tag the task's current inputs build to on
    ``base_image``, the backend's default image. ``recorded_is_reference`` says whether the
    resumed run's recorded image is such a tag (``False``: a bare image ID
    from before references). ``recorded_present`` says whether the recorded
    image is in the local daemon; ``None`` on a fresh launch.
    """

    reference: str
    base_image: str | None
    recorded_is_reference: bool = False
    recorded_present: bool | None = None


@dataclass(frozen=True)
class TaskImageBuildStep:
    """Build the task Dockerfile on ``base_image``.

    ``expected_id`` is the image ID a pre-reference run recorded, which the
    build must reproduce; ``None`` otherwise.
    """

    dockerfile: Path
    base_image: str | None
    expected_id: str | None = None


@dataclass(frozen=True)
class EditorImagePlan:
    """What a launch runs and builds for its agent container, decided before any build.

    ``image`` is the image the environment runs (``None``: the backend's
    default, or, while only validating, not yet known). ``build`` is the
    task image build to perform first, if any. ``notice`` is what the launch
    reports about a rebuild on resume.
    """

    image: str | None
    task_dockerfile: Path | None = None
    build: TaskImageBuildStep | None = None
    notice: str | None = None

    def resolve(self, built: str | None) -> str | None:
        """Return the image the environment runs, given the ID the build produced.

        ``built`` is ``None`` when no build ran. A pre-reference run refuses a
        rebuilt ID different from the recorded one.
        """
        if self.build is None or built is None:
            return self.image
        expected = self.build.expected_id
        if expected is not None and built != expected:
            raise TaskImageError.legacy_image_changed(self.build.dockerfile, expected, built)
        return self.image


def editor_image_plan(
    environment: str,
    *,
    task_dockerfile: Path | None,
    image: str | None,
    launch: Literal["fresh", "resume"],
    facts: TaskImageFacts | None,
) -> EditorImagePlan:
    """Decide which image *environment*'s agent container runs and what to build first.

    ``image`` is the operator's ``--docker-image`` on a fresh launch, or the
    image the run record holds on resume. ``facts`` is
    ``None`` while only validating: the refusals still apply, nothing is built.

    Raises :class:`TaskImageError` when the task Dockerfile cannot be honored:
    an environment with no agent container, an explicit image beside it, a
    resumed run recorded without one, or changed inputs. ``ValueError`` names
    an unknown environment. Nothing is ever silently ignored.
    """
    in_container = runs_agent_in_container(environment)
    if task_dockerfile is None:
        return EditorImagePlan(image=image)
    if not in_container:
        raise TaskImageError.no_container(environment, task_dockerfile)
    if launch == "fresh":
        return _fresh_plan(task_dockerfile, image=image, facts=facts)
    if image is None:
        raise TaskImageError.recorded_without_task_image(environment, task_dockerfile)
    reuse = EditorImagePlan(image=image, task_dockerfile=task_dockerfile)
    if facts is None:
        return reuse
    if facts.recorded_is_reference and image != facts.reference:
        raise TaskImageError.inputs_changed(task_dockerfile, image, facts.reference)
    if facts.recorded_present:
        return reuse
    notice = (
        f"[environment] the run's task image {image} is no longer in the local Docker daemon; "
        f"rebuilding it from {task_dockerfile}"
    )
    if facts.recorded_is_reference:
        step = TaskImageBuildStep(task_dockerfile, facts.base_image)
    else:
        # Built before references, without BASE_IMAGE: rebuild exactly as then.
        step = TaskImageBuildStep(task_dockerfile, None, expected_id=image)
    return EditorImagePlan(image=image, task_dockerfile=task_dockerfile, build=step, notice=notice)


def _fresh_plan(
    task_dockerfile: Path,
    *,
    image: str | None,
    facts: TaskImageFacts | None,
) -> EditorImagePlan:
    """A fresh launch builds the task image and runs its reference."""
    if image is not None:
        raise TaskImageError.operator_image(task_dockerfile, image)
    if facts is None:
        return EditorImagePlan(image=None, task_dockerfile=task_dockerfile)
    return EditorImagePlan(
        image=facts.reference,
        task_dockerfile=task_dockerfile,
        build=TaskImageBuildStep(task_dockerfile, facts.base_image),
    )


def require_built_task_image(spec: RunEnvironmentSpec, task_dockerfile: Path | None) -> None:
    """Refuse to open a run whose task image was not built into its selection.

    A task Dockerfile in a container environment needs the task image as the
    spec's ``image``; a selection built only for validation has none.
    """
    if task_dockerfile is None or not runs_agent_in_container(spec.name):
        return
    if spec.task_dockerfile != task_dockerfile or not spec.options.get("image"):
        raise TaskImageError.not_built(spec.name, task_dockerfile)
