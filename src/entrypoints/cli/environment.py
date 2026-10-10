"""Run-environment spec resolution and profiler compatibility validation."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
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

    @classmethod
    def build_timeout_unsupported(cls, environment: str) -> RunEnvironmentSelectionError:
        """Describe an image build limit for an environment that does not build locally."""
        return cls(
            f"--agent-image-build-timeout is not supported by the {environment} run environment"
        )


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


def _build_timeout_options(build_timeout: float | None, environment: str) -> dict[str, object]:
    """Return the spec option for the operator's image build limit, if one was given."""
    if build_timeout is None:
        return {}
    if environment in {"modal", "skypilot"}:
        raise RunEnvironmentSelectionError.build_timeout_unsupported(environment)
    return {"build_timeout_seconds": build_timeout}


def _task_docker_conflicts(
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


def _built_task_image(
    args: argparse.Namespace,
    dockerfile_path: Path | None,
    requested_environment: str,
    build_timeout: float | None,
    *,
    build_task_docker_image: bool,
) -> str | None:
    """Build the task's own image when this launch owns it, or return ``None``."""
    # A resumed run with no recorded image (such as one migrated from the retired
    # host environment) keeps it: building one would contradict the record.
    keeps_recorded_image = getattr(args, "resume", None) is not None and args.docker_image is None
    if (
        dockerfile_path is None
        or requested_environment != "docker"
        or not build_task_docker_image
        or keeps_recorded_image
    ):
        return None
    if build_timeout is None:
        return build_task_image(dockerfile_path)
    return build_task_image(dockerfile_path, timeout=build_timeout)


def run_environment_spec_from_args(
    args: argparse.Namespace,
    *,
    build_task_docker_image: bool = False,
) -> RunEnvironmentSpec:
    """Resolve the requested run environment from CLI and external operator config."""
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
    if (dockerfile_path is not None or docker_in_docker) and not resuming:
        conflicts = _task_docker_conflicts(requested_environment, selected, explicit)
        if conflicts:
            joined = ", ".join(conflicts)
            declared = (
                f"task Dockerfile {dockerfile_path}"
                if dockerfile_path is not None
                else "task key [environment] docker_in_docker = true"
            )
            _exception_message_2 = f"{declared} cannot be combined with {joined}"
            raise ValueError(_exception_message_2)

    build_timeout = getattr(args, "agent_image_build_timeout", None)
    timeout_options = _build_timeout_options(build_timeout, requested_environment)

    task_image = _built_task_image(
        args,
        dockerfile_path,
        requested_environment,
        build_timeout,
        build_task_docker_image=build_task_docker_image,
    )

    if requested_environment == "slurm-gpu":
        return RunEnvironmentSpec(
            name="slurm-gpu",
            options={
                "config_path": str(
                    slurm_config or Path("~/.config/vibesys/slurm-gpu.toml").expanduser()
                ),
                **timeout_options,
            },
            resources=bundle.manifest.resources if bundle is not None else None,
        )
    if requested_environment == "slurm":
        return RunEnvironmentSpec(
            name="slurm",
            options={
                "config_path": str(
                    slurm_config or Path("~/.config/vibesys/slurm.toml").expanduser()
                ),
                **timeout_options,
            },
        )

    spec = make_run_environment_spec(
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
    if timeout_options:
        spec = replace(spec, options={**spec.options, **timeout_options})
    return spec


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
