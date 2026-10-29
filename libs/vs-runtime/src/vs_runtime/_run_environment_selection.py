"""Selecting, recording, and building run environments from a spec.

``RunEnvironmentSpec`` is the CLI/config selection; this module projects it onto
the persisted record, derives the host environment for host-only backends, and
builds the concrete ``RunEnvironment``.  It sits above ``_run_environment``,
which defines the environments themselves.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from vs_project.api import RunEnvironmentRecord, RunResourceRequest
from vs_runtime._run_environment import (
    DockerEnvironment,
    DockerEnvironmentConfig,
    HostEnvironment,
    ModalEnvironment,
    RunEnvironment,
    RunEnvironmentSpec,
    SkyPilotEnvironment,
)
from vs_runtime._slurm_environment import SlurmEnvironment
from vs_runtime._task_image_policy import TaskImageError, editor_image_plan
from vs_sandbox.api import backend_is_host_only

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.api import ComputeBackend

_RunEnvironmentName = Literal["docker", "host", "modal", "skypilot", "slurm", "slurm-gpu"]
_RECORDED_ENVIRONMENT_NAMES: tuple[_RunEnvironmentName, ...] = (
    "docker",
    "host",
    "modal",
    "skypilot",
    "slurm",
    "slurm-gpu",
)


def resolve_run_environment_spec(
    spec: RunEnvironmentSpec,
    backend: ComputeBackend,
    *,
    task_dockerfile: Path | None,
    platform: str,
    log: Callable[[str], None],
) -> RunEnvironmentSpec:
    """Select the host environment for a backend containers cannot serve.

    The default Docker selection becomes ``host`` exactly when the backend
    declares itself host-only and ``platform`` (a ``sys.platform`` value) is
    macOS, where Seatbelt can confine it.  The choice follows from the backend
    alone, never from a flag, and every other spec is returned unchanged.

    ``task_dockerfile`` is the Dockerfile the run's task declares. A spec not
    built from it raises :class:`TaskImageError`, so no caller can open a run
    that silently ignores the task image; nor can the host environment, which
    has no container to build it into.
    """
    if task_dockerfile is not None and spec.task_dockerfile != task_dockerfile:
        raise TaskImageError.not_built(spec.name, task_dockerfile)
    derived = derived_environment_name(spec.name, backend, platform=platform)
    if task_dockerfile is not None:
        # The same decision every environment makes refuses the task image in
        # an environment without an agent container, selected or derived.
        editor_image_plan(
            derived, task_dockerfile=task_dockerfile, image=None, launch="fresh", facts=None
        )
    if derived == spec.name:
        return spec
    log(
        f"[environment] the {backend.value} backend cannot be reached from Docker; "
        "running the agent on the host under Seatbelt"
    )
    return replace(spec, name="host", options={}, image_id=None)


def derived_environment_name(name: str, backend: ComputeBackend, *, platform: str) -> str:
    """Return the environment a *name* selection runs in on *platform* with *backend*.

    ``docker`` becomes ``host`` for a host-only backend on macOS; every other
    selection is itself. :func:`resolve_run_environment_spec` applies it; an
    entry point asks it before building a task image the host would refuse.
    """
    if name == "docker" and platform == "darwin" and backend_is_host_only(backend):
        return "host"
    return name


def run_environment_deprecation(environment: RunEnvironment) -> str | None:
    """Return the deprecation notice of a built environment, or ``None``.

    A run reports it once when it launches. Opening an environment (done for
    every candidate session) must not, or the notice repeats.
    """
    return environment.deprecation if isinstance(environment, SlurmEnvironment) else None


def run_environment_record(spec: RunEnvironmentSpec) -> RunEnvironmentRecord:
    """Project a CLI-built spec onto the record persisted with the run.

    Only operator-selected options are recorded, under the spec's own option
    names. The candidate's Modal entrypoint is deliberately excluded: it is
    declared by the input bundle and re-derived on every launch, so recording
    it would make a legitimate task edit look like a resume mismatch.
    """
    return RunEnvironmentRecord(
        name=_recorded_environment_name(spec.name),
        image=_recorded_option(spec, "image"),
        gpu=_recorded_option(spec, "gpu"),
        model_volume=_recorded_option(spec, "model_volume"),
        app=_recorded_option(spec, "app"),
        config_path=_recorded_option(spec, "config_path"),
        resources=spec.resources,
    )


def _recorded_environment_name(name: str) -> _RunEnvironmentName:
    """Validate a spec name against the environments the run record can hold."""
    for recorded in _RECORDED_ENVIRONMENT_NAMES:
        if name == recorded:
            return recorded
    message = f"unknown run environment: {name!r}"
    raise ValueError(message)


def _recorded_option(spec: RunEnvironmentSpec, key: str) -> str | None:
    value = spec.options.get(key)
    return str(value) if value else None


def build_run_environment(spec: RunEnvironmentSpec) -> RunEnvironment:
    """Construct the implementation selected by a run environment spec."""
    if spec.name == "local":
        message = (
            "the local agent environment was removed: agents always run in Docker "
            "(use the docker run environment)"
        )
        raise ValueError(message)
    # The environment runs the image's pinned ID when the launch resolved one.
    options = spec.options if spec.image_id is None else {**spec.options, "image": spec.image_id}
    if spec.name == "docker":
        return DockerEnvironment.from_options(options)
    if spec.name == "host":
        return HostEnvironment.from_options(options)
    if spec.name == "modal":
        return ModalEnvironment.from_options(options)
    if spec.name == "skypilot":
        return SkyPilotEnvironment.from_options(options, spec.resources)
    if spec.name == "slurm":
        return SlurmEnvironment.from_options(options)
    if spec.name == "slurm-gpu":
        # Deprecated alias: the old file, translated, opens the slurm environment.
        image = options.get("image")
        return SlurmEnvironment.from_slurm_gpu(
            Path(str(options["config_path"])),
            spec.resources,
            docker=DockerEnvironmentConfig(image=str(image) if image else None),
        )
    message = f"unknown run environment: {spec.name!r}"
    raise ValueError(message)


def validate_run_environment_profile(
    environment: RunEnvironment, profile_command: tuple[str, ...] | None
) -> None:
    """Validate an enabled profiler's workload without provisioning resources.

    Configured Slurm services require a trusted profile command. Other
    environments and Slurm jobs without a service allow an absent command.
    Invalid operator policy or workload requirements raise ``ValueError``.
    """
    if isinstance(environment, SlurmEnvironment):
        environment.validate_profile(profile_command)


def make_run_environment_spec(  # noqa: PLR0913  # lint-waiver: LW-009086 [PLR0913]; the compatibility builder accepts each independent CLI environment option.
    *,
    docker_image: str | None = None,
    use_modal: bool = False,
    modal_gpu: str = "H100!",
    modal_model_volume: str | None = None,
    modal_app: str = "vibesys",
    modal_entrypoint: str | None = None,
    use_skypilot: bool = False,
    cluster_profile: str | None = None,
    cluster_profiles_file: Path | None = None,
    skypilot_executable: str = "sky",
    resources: RunResourceRequest | None = None,
) -> RunEnvironmentSpec:
    """Build a spec from the current CLI compatibility flags.

    Modal mode (April 2026 refactor) runs the agent in a *local Docker
    container* and dispatches GPU work via the candidate's ``modal run`` entrypoint,
    so the legacy long-lived-Modal-sandbox knobs (timeout / idle_timeout)
    no longer apply here — they live on the implementer's per-function
    ``@app.function(timeout=...)`` / ``@app.cls(container_idle_timeout=...)``
    decorators instead.
    """
    if sum((use_modal, use_skypilot)) > 1:
        message = "--modal and --skypilot are mutually exclusive"
        raise ValueError(message)
    if use_skypilot:
        if not cluster_profile:
            message = "--skypilot requires --cluster-profile"
            raise ValueError(message)
        if resources is None:
            message = "--skypilot requires input [resources]"
            raise ValueError(message)
        return RunEnvironmentSpec(
            name="skypilot",
            options={
                "image": docker_image,
                "profile": cluster_profile,
                "profiles_file": cluster_profiles_file,
                "executable": skypilot_executable,
            },
            resources=resources,
        )
    if use_modal:
        options: dict[str, object] = {
            "image": docker_image,
            "gpu": modal_gpu,
            "model_volume": modal_model_volume,
            "app": modal_app,
        }
        if modal_entrypoint is not None:
            options["entrypoint"] = modal_entrypoint
        return RunEnvironmentSpec(
            name="modal",
            options=options,
            resources=resources,
        )
    return RunEnvironmentSpec(name="docker", options={"image": docker_image}, resources=resources)
