"""Run-environment spec resolution and profiler compatibility validation."""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from entrypoints.cli.errors import _configuration_error
from vibesys.api.request import (
    RunEnvironmentSpec,
    TaskImageFacts,
    backend_default_image,
    build_task_image,
    derived_environment_name,
    editor_image_plan,
    is_task_image_reference,
    local_image_id,
    make_run_environment_spec,
    supported_profilers,
    task_image_inputs,
    task_image_reference,
)
from vs_agent.api.images import SubprocessDockerBuildRunner
from vs_sim.api import DISCARD, MonotonicClock

if TYPE_CHECKING:
    import argparse

    from vibesys.api import ComputeBackend
    from vibesys.api.request import DockerBuildRunner, EditorImagePlan


class RunEnvironmentSelectionError(ValueError):
    """An incompatible set of run environment selectors."""

    @classmethod
    def slurm_requires_selection(cls) -> RunEnvironmentSelectionError:
        """Describe a config path paired with another environment."""
        return cls("--slurm-config requires --run-environment slurm or slurm-gpu")

    @classmethod
    def slurm_conflict(cls) -> RunEnvironmentSelectionError:
        """Describe a Slurm config paired with a compatibility selector."""
        return cls("--slurm-config cannot be combined with --modal or --skypilot")


def _requested_environment(
    args: argparse.Namespace, selected: str | None, slurm_config: Path | None
) -> str:
    return (
        selected
        or ("skypilot" if getattr(args, "skypilot", False) else None)
        or ("modal" if args.modal else None)
        or ("slurm" if slurm_config is not None else None)
        or "docker"
    )


def _docker_in_docker_conflicts(
    requested: str, selected: str | None, explicit: frozenset[str]
) -> list[str]:
    conflicts: list[str] = []
    if requested in {"modal", "skypilot", "slurm", "slurm-gpu"}:
        conflicts.append(
            f"--run-environment {requested}"
            if selected
            else ("--slurm-config" if requested == "slurm" else f"--{requested}")
        )
    if "docker_image" in explicit:
        conflicts.append("--docker-image")
    return conflicts


@dataclass(frozen=True)
class TaskImageBuild:
    """Build the task Dockerfile, when there is one, for a launch on *backend*.

    The Dockerfile is built on *backend*'s default image, passed as
    ``BASE_IMAGE``. *runner* runs the Docker commands; ``None`` runs the real
    Docker CLI. *platform* is the host platform the run opens on, which decides
    whether a host-only backend derives the host environment. ``allow_build=False``
    resolves a saved image for a reader without rebuilding a missing one.
    """

    backend: ComputeBackend
    runner: DockerBuildRunner | None = None
    platform: str = sys.platform
    allow_build: bool = True


def _task_image_facts(
    dockerfile: Path,
    recorded: str | None,
    launch: Literal["fresh", "resume"],
    build: TaskImageBuild,
) -> TaskImageFacts:
    """Read what the plan needs: the inputs' reference and whether the recorded image is local."""
    base_image = backend_default_image(build.backend)
    reference = task_image_reference(dockerfile, base_image=base_image)
    if launch == "fresh" or recorded is None:
        return TaskImageFacts(reference=reference, base_image=base_image)
    return TaskImageFacts(
        reference=reference,
        base_image=base_image,
        recorded_is_reference=is_task_image_reference(recorded),
        recorded_inputs_match=task_image_inputs(recorded) == task_image_inputs(reference),
        recorded_id=local_image_id(
            recorded,
            command_runner=build.runner or SubprocessDockerBuildRunner(),
            output=DISCARD,
            clock=MonotonicClock(),
        ),
    )


def _editor_image(
    environment: str,
    *,
    dockerfile: Path | None,
    image: str | None,
    launch: Literal["fresh", "resume"],
    build: TaskImageBuild | None,
) -> EditorImagePlan:
    """Decide the editor image and perform the build the decision asks for.

    The refusals run first, without reading or building anything, so an
    environment that cannot honor the task image fails before a build.
    """
    plan = editor_image_plan(
        environment, task_dockerfile=dockerfile, image=image, launch=launch, facts=None
    )
    if dockerfile is None or build is None:
        return plan
    plan = editor_image_plan(
        environment,
        task_dockerfile=dockerfile,
        image=image,
        launch=launch,
        facts=_task_image_facts(dockerfile, image, launch, build),
    )
    if plan.build is not None and not build.allow_build:
        message = f"the saved task image {image} is unavailable; a chat reader cannot rebuild it"
        raise ValueError(message)
    if plan.notice is not None:
        sys.stderr.write(f"{plan.notice}\n")
    if plan.build is None:
        return plan
    if plan.notice is None:
        sys.stderr.write(_building_notice(plan.build.dockerfile))
    built = build_task_image(
        plan.build.dockerfile,
        base_image=plan.build.base_image,
        command_runner=build.runner,
        clock=MonotonicClock(),
        output=DISCARD,
    )
    return replace(plan, image_id=plan.resolve(built))


def _building_notice(dockerfile: Path) -> str:
    """Tell the user a task image build is starting, which prints nothing until it ends."""
    return (
        f"[environment] building the task image from {dockerfile}; the first build downloads "
        f"the task's toolchain and can take several minutes, and later runs reuse it\n"
    )


def _image_option(image: str | None) -> dict[str, object]:
    """The Slurm editor's ``image`` option, present only when there is one to pin."""
    return {} if image is None else {"image": image}


def run_environment_spec_from_args(
    args: argparse.Namespace,
    *,
    task_image: TaskImageBuild | None = None,
) -> RunEnvironmentSpec:
    """Resolve the requested run environment from CLI and external operator config.

    With *task_image*, a task Dockerfile is built (or, on resume, reused); its
    reference becomes the recorded ``image`` and its immutable ID the spec's
    ``image_id``, which the environment runs. Without it (validation) nothing
    is built, but the same decision runs, so an unsupported combination fails
    either way.
    """
    bundle = getattr(args, "input_bundle", None)
    selected = getattr(args, "run_environment", None)
    explicit = getattr(args, "explicit_cli_dests", frozenset())
    if "run_environment" in explicit and {"modal", "skypilot"} & explicit:
        message = "--run-environment cannot be combined with --modal or --skypilot"
        raise ValueError(message)
    compatibility_selections = (
        args.modal,
        getattr(args, "skypilot", False),
    )
    slurm_config = getattr(args, "slurm_config", None)
    if selected not in {None, "slurm", "slurm-gpu"} and slurm_config is not None:
        raise RunEnvironmentSelectionError.slurm_requires_selection()
    if slurm_config is not None and any(compatibility_selections):
        raise RunEnvironmentSelectionError.slurm_conflict()
    if sum(compatibility_selections) > 1:
        _exception_message = "--modal and --skypilot are mutually exclusive"
        raise ValueError(_exception_message)

    dockerfile_path = bundle.dockerfile_path if bundle is not None else None
    docker_in_docker = bundle is not None and bundle.docker_in_docker
    resuming = getattr(args, "resume", None) is not None
    requested_environment = _requested_environment(args, selected, slurm_config)
    if docker_in_docker and not resuming:
        conflicts = _docker_in_docker_conflicts(requested_environment, selected, explicit)
        if conflicts:
            joined = ", ".join(conflicts)
            _exception_message_2 = (
                f"task key [environment] docker_in_docker = true cannot be combined with {joined}"
            )
            raise ValueError(_exception_message_2)

    # The environment the run will open: Metal on macOS derives the host one,
    # which refuses a task image, so ask before building it.
    plan = _editor_image(
        (
            derived_environment_name(
                requested_environment, task_image.backend, platform=task_image.platform
            )
            if task_image is not None
            else requested_environment
        ),
        dockerfile=dockerfile_path,
        image=args.docker_image,
        launch="resume" if resuming else "fresh",
        build=task_image,
    )
    image = plan.image

    if requested_environment == "slurm-gpu":
        return RunEnvironmentSpec(
            name="slurm-gpu",
            options={
                "config_path": str(
                    slurm_config or Path("~/.config/vibesys/slurm-gpu.toml").expanduser()
                ),
                **_image_option(image),
            },
            resources=bundle.manifest.resources if bundle is not None else None,
            task_dockerfile=plan.task_dockerfile,
            image_id=plan.image_id,
        )
    if requested_environment == "slurm":
        return RunEnvironmentSpec(
            name="slurm",
            options={
                "config_path": str(
                    slurm_config or Path("~/.config/vibesys/slurm.toml").expanduser()
                ),
                **_image_option(image),
            },
            task_dockerfile=plan.task_dockerfile,
            image_id=plan.image_id,
        )

    return replace(
        make_run_environment_spec(
            docker_image=image,
            use_modal=requested_environment == "modal",
            modal_gpu=args.modal_gpu,
            modal_model_volume=args.modal_model_volume,
            modal_app=args.modal_app,
            modal_entrypoint=bundle.modal_entrypoint if bundle is not None else None,
            use_skypilot=requested_environment == "skypilot",
            cluster_profile=getattr(args, "cluster_profile", None),
            cluster_profiles_file=getattr(args, "cluster_profiles_file", None),
            skypilot_executable=getattr(args, "skypilot_executable", "sky"),
            resources=bundle.manifest.resources if bundle is not None else None,
        ),
        task_dockerfile=plan.task_dockerfile,
        image_id=plan.image_id,
    )


def _validate_run_environment_profiler(args: argparse.Namespace) -> None:
    """Validate profiler compatibility through the selected adapter contract."""
    spec = run_environment_spec_from_args(args)
    supported = supported_profilers(spec)
    if supported is None or args.profiler in supported:
        return
    allowed = ", ".join(sorted(kind.value for kind in supported))
    _configuration_error(
        f"Error: run environment {spec.name!r} does not support "
        f"--profiler={args.profiler.value}; allowed: {allowed}."
    )
