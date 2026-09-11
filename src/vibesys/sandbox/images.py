"""Build the two Docker images a task runs from.

A task runs from an *agent image*: the shipped CLIs, helper tools, and a
non-root ``agent`` user layered on top of a *task image* (built from the
task's own Dockerfile, when it has one) or directly on a backend base image.
Task images stay pure and serve the evaluator; the agent layer is what a CLI
provider actually runs in, so a CLI version bump rebuilds only that top
layer.

Docker owns layer caching for both images; VibeSys invokes each build once
per launch and consumes the runnable manifest ID resolved from a unique local
tag. Default provenance attestations are disabled because their generated
metadata otherwise changes the manifest ID when all filesystem layers match.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vibesys.agents import provider_policy

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

_DEFAULT_BUILD_TIMEOUT_SECONDS = 1200.0
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_DIAGNOSTIC_LIMIT = 1000
_TASK_IMAGE_REPOSITORY = "vibesys-task-build"
_AGENT_IMAGE_REPOSITORY = "vibesys-agent-build"

#: The agent layer's build context: its Dockerfile plus nothing else. Resolved
#: relative to this module rather than through ``importlib.resources`` so the
#: path is always a real filesystem path Docker can build from, whether this
#: package runs from a source checkout or an installed wheel (package data is
#: unpacked to disk, never zipped, for this project).
_AGENT_IMAGE_DIR = Path(__file__).resolve().parent / "images"
_AGENT_DOCKERFILE = _AGENT_IMAGE_DIR / "agent.Dockerfile"


class TaskImageBuildError(RuntimeError):
    """Raised when Docker cannot produce a valid immutable image."""


@dataclass(frozen=True)
class _BuildTarget:
    """Where a Dockerfile and its build context are, and how to tag and name it.

    Bundled so the build/inspect/error-reporting helpers below stay under the
    project's argument-count limit now that this module builds two images.
    """

    dockerfile: Path
    context: Path
    image_repository: str
    image_label: str


class DockerBuildRunner(Protocol):
    """Injectable process boundary for a Docker image build."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """Run one Docker command without a shell."""
        ...


class SubprocessDockerBuildRunner:
    """Run Docker directly and capture its diagnostics."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """Run one Docker command without invoking a shell."""
        return subprocess.run(  # noqa: S603
            tuple(argv),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )


def build_task_image(
    dockerfile_path: Path,
    *,
    base_image: str | None = None,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> str:
    """Build a task Dockerfile and return its immutable Docker image ID.

    The task directory is the complete Docker build context. Candidate source,
    Git metadata, and other project inputs therefore cannot be copied into the
    image. Docker owns layer caching; VibeSys invokes the build once per launch
    and consumes the runnable manifest ID resolved from a unique local tag.
    Default provenance attestations are disabled because their generated
    metadata otherwise changes the manifest ID when all filesystem layers
    match. The unique tag remains as a local reachability anchor: containerd's
    image store cannot run the stable config digest emitted by ``--iidfile``
    when an otherwise untagged image has no retained manifest reference.

    ``base_image``, when given, is passed as ``--build-arg BASE_IMAGE=...``,
    so a task Dockerfile may declare ``ARG BASE_IMAGE`` and
    ``FROM ${BASE_IMAGE}`` to build on top of the backend's base image instead
    of naming its own. A task Dockerfile with its own ``FROM`` line ignores an
    unused build arg, so this is safe to pass unconditionally from a caller
    that does not know whether the task Dockerfile uses it.
    """
    # Keep the lexical parent as the build context. Resolving the Dockerfile
    # itself could silently widen the context to a symlink target elsewhere.
    dockerfile = dockerfile_path.expanduser().absolute()
    root = dockerfile.parent
    if not dockerfile.is_file() or dockerfile.is_symlink():
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Task Dockerfile must be a regular file: {dockerfile}"
        )
    if timeout <= 0:
        raise ValueError("task image build timeout must be positive")  # noqa: TRY003

    runner = command_runner or SubprocessDockerBuildRunner()
    extra_build_args = ("--build-arg", f"BASE_IMAGE={base_image}") if base_image is not None else ()
    target = _BuildTarget(
        dockerfile=dockerfile,
        context=root,
        image_repository=_TASK_IMAGE_REPOSITORY,
        image_label="task image",
    )
    return _build_and_inspect(target, extra_build_args, runner=runner, timeout=timeout)


def agent_image(
    base_image: str,
    *,
    task_dockerfile: Path | None = None,
    toolchains: Collection[str] = (),
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> str:
    """Build the agent layer on top of a task image, and return its image ID.

    When ``task_dockerfile`` is given, the task image is built first (via
    :func:`build_task_image`, with ``base_image`` passed through so the task
    Dockerfile may ``FROM ${BASE_IMAGE}``) and the agent layer is built on top
    of the resulting image ID. Otherwise the agent layer is built directly on
    ``base_image``.

    CLI and toolchain versions come from :mod:`vibesys.agents.provider_policy`
    and are passed as build args, so a version bump changes this module's
    caller nowhere: rebuilding with an unchanged Dockerfile and unchanged
    build args resolves to the same image from Docker's layer cache.
    ``toolchains`` is sorted and deduplicated before being rendered, so the
    build args (and therefore the cache key) do not depend on collection
    order.
    """
    if timeout <= 0:
        raise ValueError("agent image build timeout must be positive")  # noqa: TRY003

    runner = command_runner or SubprocessDockerBuildRunner()
    base = (
        build_task_image(
            task_dockerfile,
            base_image=base_image,
            command_runner=runner,
            timeout=timeout,
        )
        if task_dockerfile is not None
        else base_image
    )

    build_args: list[str] = ["--build-arg", f"BASE_IMAGE={base}"]
    build_args += ["--build-arg", f"NODE_VERSION={provider_policy.NODE_VERSION}"]
    for provider, version in provider_policy.CLI_VERSIONS.items():
        build_args += ["--build-arg", f"{provider.upper()}_VERSION={version}"]
    build_args += ["--build-arg", f"TOOLCHAINS={' '.join(sorted(set(toolchains)))}"]
    build_args += ["--build-arg", f"RUST_VERSION={provider_policy.RUST_TOOLCHAIN_VERSION}"]
    build_args += ["--build-arg", f"GO_VERSION={provider_policy.GO_TOOLCHAIN_VERSION}"]

    target = _BuildTarget(
        dockerfile=_AGENT_DOCKERFILE,
        context=_AGENT_IMAGE_DIR,
        image_repository=_AGENT_IMAGE_REPOSITORY,
        image_label="agent image",
    )
    return _build_and_inspect(target, tuple(build_args), runner=runner, timeout=timeout)


def _build_and_inspect(
    target: _BuildTarget,
    extra_build_args: Sequence[str],
    *,
    runner: DockerBuildRunner,
    timeout: float,
) -> str:
    """Build one Dockerfile to a unique tag and resolve its immutable ID."""
    image_tag = f"{target.image_repository}:{uuid.uuid4().hex}"
    build_argv = (
        "docker",
        "build",
        "--provenance=false",
        "--tag",
        image_tag,
        "--file",
        str(target.dockerfile),
        *extra_build_args,
        str(target.context),
    )
    build_result = _run_docker(
        runner, build_argv, timeout=timeout, action="building", target=target
    )
    if build_result.returncode != 0:
        detail = (build_result.stderr or build_result.stdout or "docker build failed").strip()
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Could not build {target.image_label} from {target.dockerfile} "
            f"(exit {build_result.returncode}): {detail[:_DIAGNOSTIC_LIMIT]}"
        )

    inspect_argv = (
        "docker",
        "image",
        "inspect",
        "--format",
        "{{.Id}}",
        image_tag,
    )
    inspect_result = _run_docker(
        runner, inspect_argv, timeout=timeout, action="inspecting", target=target
    )
    if inspect_result.returncode != 0:
        detail = (inspect_result.stderr or inspect_result.stdout or "docker inspect failed").strip()
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Could not resolve runnable {target.image_label} {image_tag} built from "
            f"{target.dockerfile} (exit {inspect_result.returncode}): "
            f"{detail[:_DIAGNOSTIC_LIMIT]}"
        )
    image_id = inspect_result.stdout.strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        displayed = image_id[:_DIAGNOSTIC_LIMIT] or "<empty>"
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Docker returned an invalid runnable {target.image_label} ID for "
            f"{target.dockerfile}: {displayed!r}"
        )
    return image_id


def _run_docker(
    runner: DockerBuildRunner,
    argv: Sequence[str],
    *,
    timeout: float,
    action: str,
    target: _BuildTarget,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(argv, cwd=target.dockerfile.parent, timeout=timeout)
    except FileNotFoundError as exc:
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Docker was not found while {action} {target.image_label}: {target.dockerfile}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TaskImageBuildError(  # noqa: TRY003  # external CLI diagnostic
            f"Docker timed out after {timeout:g} seconds while {action} "
            f"{target.image_label}: {target.dockerfile}"
        ) from exc
