"""Run-environment spec resolution and profiler compatibility validation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.cli.errors import _configuration_error
from vibesys.api.request import (
    RunEnvironmentSpec,
    build_task_image,
    make_run_environment_spec,
    supported_profilers,
)

if TYPE_CHECKING:
    import argparse


def run_environment_spec_from_args(
    args: argparse.Namespace,
    *,
    build_task_docker_image: bool = False,
) -> RunEnvironmentSpec:
    """Resolve the requested run environment (local/docker/modal/skypilot) from CLI args."""
    bundle = getattr(args, "input_bundle", None)
    selected = getattr(args, "run_environment", None)
    explicit = getattr(args, "explicit_cli_dests", frozenset())
    if "run_environment" in explicit and {"docker", "modal", "skypilot"} & explicit:
        message = "--run-environment cannot be combined with --docker, --modal, or --skypilot"
        raise ValueError(message)
    compatibility_selections = (
        args.docker,
        args.modal,
        getattr(args, "skypilot", False),
    )
    if sum(compatibility_selections) > 1:
        _exception_message = "--docker, --modal, and --skypilot are mutually exclusive"
        raise ValueError(_exception_message)

    dockerfile_path = bundle.dockerfile_path if bundle is not None else None
    resuming = getattr(args, "resume", None) is not None
    requested_environment = (
        selected
        or ("skypilot" if getattr(args, "skypilot", False) else None)
        or ("modal" if args.modal else None)
        or ("docker" if args.docker else None)
        or "local"
    )
    if dockerfile_path is not None and not resuming:
        conflicts: list[str] = []
        if requested_environment == "local" and "run_environment" in explicit:
            conflicts.append("--run-environment local")
        elif requested_environment == "modal":
            conflicts.append("--run-environment modal" if selected else "--modal")
        elif requested_environment == "skypilot":
            conflicts.append("--run-environment skypilot" if selected else "--skypilot")
        if "docker_image" in explicit:
            conflicts.append("--docker-image")
        if conflicts:
            joined = ", ".join(conflicts)
            _exception_message_2 = (
                f"task Dockerfile {dockerfile_path} cannot be combined with {joined}"
            )
            raise ValueError(_exception_message_2)
        requested_environment = "docker"

    task_image = None
    if (
        dockerfile_path is not None
        and requested_environment == "docker"
        and build_task_docker_image
    ):
        task_image = build_task_image(dockerfile_path)

    return make_run_environment_spec(
        use_docker=requested_environment == "docker",
        docker_image=task_image or args.docker_image,
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
