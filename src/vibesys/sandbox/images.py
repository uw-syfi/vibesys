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

import json
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
_DEFAULT_PUSH_TIMEOUT_SECONDS = 1200.0
_DEFAULT_VERIFY_TIMEOUT_SECONDS = 60.0
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_DIAGNOSTIC_LIMIT = 1000
_TASK_IMAGE_REPOSITORY = "vibesys-task-build"
_AGENT_IMAGE_REPOSITORY = "vibesys-agent-build"

#: Where a pushed agent image lives. GHCR because the repository is on GitHub
#: and it is cloud-neutral for SkyPilot's arbitrary infra targets. Runs
#: reference the resulting digest, never this repository's moving tags.
DEFAULT_AGENT_IMAGE_REGISTRY = "ghcr.io/uw-syfi/vibesys-agent"

#: Length of the content-derived tag `push_agent_image` pushes under. Not a
#: security boundary, just short enough to read in logs while still keying
#: the tag off the image content, so re-pushing the same image ID reuses the
#: same tag and Docker's own layer-presence check makes the push itself a
#: fast no-op.
_SHORT_ID_LENGTH = 12

#: The agent layer's build context: its Dockerfile plus nothing else. Resolved
#: relative to this module rather than through ``importlib.resources`` so the
#: path is always a real filesystem path Docker can build from, whether this
#: package runs from a source checkout or an installed wheel (package data is
#: unpacked to disk, never zipped, for this project).
_AGENT_IMAGE_DIR = Path(__file__).resolve().parent / "images"
_AGENT_DOCKERFILE = _AGENT_IMAGE_DIR / "agent.Dockerfile"


class TaskImageBuildError(RuntimeError):
    """Raised when Docker cannot produce a valid immutable image."""


class ImagePushError(RuntimeError):
    """Raised when an agent image cannot be pushed to, or verified in, a registry."""


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


def agent_image(  # noqa: PLR0913  # tracked: #288
    base_image: str,
    *,
    task_dockerfile: Path | None = None,
    toolchains: Collection[str] = (),
    pip_extras: Collection[str] = (),
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_BUILD_TIMEOUT_SECONDS,
) -> str:
    """Build the agent layer on top of a task image, and return its image ID.

    ``pip_extras`` are extra pip requirements an execution environment needs
    inside the editor container (sorted and deduplicated like ``toolchains``).

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
    build_args += ["--build-arg", f"PIP_EXTRAS={' '.join(sorted(set(pip_extras)))}"]

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


# ---------------------------------------------------------------------------
# Registry push and verification.
#
# A local ``--docker`` run never calls any of the functions below: it runs
# the image ``agent_image`` just built directly from the local Docker image
# store. Only a remote backend (Modal, SkyPilot) needs the image pulled from
# somewhere else, so only those callers push. A push is keyed by the local
# image ID rather than by tag: ``ensure_pushed`` checks whether *this exact*
# content was already pushed to *repository* before running `docker push`
# again, so a resumed or repeated remote launch against an unchanged agent
# image is a no-op past the first launch.
#
# Prerequisite, out of scope here: the caller's Docker daemon must already be
# logged in to the registry (``docker login ghcr.io`` with a token carrying
# `write:packages`; CI supplies this as `GITHUB_TOKEN`). Nothing in this
# module performs or inspects that login, and no function here logs
# credentials, only image references and exit codes.
# ---------------------------------------------------------------------------


def push_agent_image(
    image_id: str,
    *,
    repository: str = DEFAULT_AGENT_IMAGE_REGISTRY,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_PUSH_TIMEOUT_SECONDS,
) -> str:
    """Tag, push, and resolve the pullable digest reference for *image_id*.

    *image_id* must be the immutable ``sha256:...`` ID :func:`agent_image` (or
    :func:`build_task_image`) returned, not a mutable tag. It is tagged as
    ``<repository>:<short id>`` (a name derived from the image's own content
    address, not a moving tag), pushed, and then resolved back to the
    ``<repository>@sha256:...`` manifest digest Docker recorded for that push,
    which is what a remote backend actually pulls.

    Always pushes, even when the registry already has this content: callers
    that want to skip a redundant push when it is already verified present
    should call :func:`ensure_pushed` instead, which checks first.

    Raises:
        ValueError: if *image_id* is not an immutable image ID, or *timeout*
            is not positive.
        ImagePushError: if Docker is missing, the tag/push/inspect step times
            out or fails, or Docker reports no usable digest for the pushed
            tag. The docker CLI's own diagnostics are included; a failed push
            is very often an expired or missing registry login, so the
            message also names the `docker login` prerequisite.
    """
    if _IMAGE_ID.fullmatch(image_id) is None:
        raise ValueError(  # noqa: TRY003
            f"push_agent_image requires an immutable image ID (sha256:...), got {image_id!r}"
        )
    if timeout <= 0:
        raise ValueError("agent image push timeout must be positive")  # noqa: TRY003

    runner = command_runner or SubprocessDockerBuildRunner()
    cwd = Path.cwd()
    tag = f"{repository}:{image_id.removeprefix('sha256:')[:_SHORT_ID_LENGTH]}"

    tag_result = _run_registry_command(
        runner, ("docker", "tag", image_id, tag), cwd=cwd, timeout=timeout, action="tagging"
    )
    if tag_result.returncode != 0:
        detail = (tag_result.stderr or tag_result.stdout or "docker tag failed").strip()
        raise ImagePushError(  # noqa: TRY003
            f"Could not tag agent image {image_id} as {tag} "
            f"(exit {tag_result.returncode}): {detail[:_DIAGNOSTIC_LIMIT]}"
        )

    push_result = _run_registry_command(
        runner, ("docker", "push", tag), cwd=cwd, timeout=timeout, action="pushing"
    )
    if push_result.returncode != 0:
        detail = (push_result.stderr or push_result.stdout or "docker push failed").strip()
        raise ImagePushError(  # noqa: TRY003
            f"Could not push agent image {tag} (exit {push_result.returncode}): "
            f"{detail[:_DIAGNOSTIC_LIMIT]}. Authenticate first with "
            "`docker login ghcr.io` using a token with `write:packages` scope "
            "(`GITHUB_TOKEN` in CI)."
        )

    inspect_result = _run_registry_command(
        runner,
        ("docker", "inspect", "--format", "{{index .RepoDigests 0}}", tag),
        cwd=cwd,
        timeout=timeout,
        action="inspecting",
    )
    digest_reference = inspect_result.stdout.strip()
    if inspect_result.returncode != 0 or not digest_reference:
        detail = (inspect_result.stderr or inspect_result.stdout or "no output").strip()
        raise ImagePushError(  # noqa: TRY003
            f"Pushed {tag} but Docker returned no repo digest for it: {detail[:_DIAGNOSTIC_LIMIT]}"
        )
    if not digest_reference.startswith(f"{repository}@sha256:"):
        raise ImagePushError(  # noqa: TRY003
            f"Docker returned an unexpected repo digest for {tag}: {digest_reference!r}"
        )
    return digest_reference


def agent_image_is_pushed(
    reference: str,
    *,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_VERIFY_TIMEOUT_SECONDS,
) -> bool:
    """Return whether *reference* (a ``repository@sha256:...`` digest) is live in its registry.

    Uses ``docker manifest inspect``, which asks the registry for the
    manifest without pulling any layers. A nonzero exit is treated uniformly
    as "not verified", whether the digest is genuinely absent, the registry
    is unreachable, or credentials are missing or expired: this function
    only answers "is it there right now", and :func:`push_agent_image`'s own
    error reporting is what surfaces which of those it was when a caller
    acts on a ``False`` result by trying to push.
    """
    runner = command_runner or SubprocessDockerBuildRunner()
    try:
        result = runner.run(
            ("docker", "manifest", "inspect", reference), cwd=Path.cwd(), timeout=timeout
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def ensure_pushed(
    image_id: str,
    *,
    repository: str = DEFAULT_AGENT_IMAGE_REGISTRY,
    command_runner: DockerBuildRunner | None = None,
    timeout: float = _DEFAULT_PUSH_TIMEOUT_SECONDS,
) -> str:
    """Return a pullable ``repository@sha256:...`` reference for *image_id*, pushing only if needed.

    Checks Docker's own record of where this exact local image was already
    pushed (``docker image inspect``'s ``RepoDigests``, which persists across
    processes as long as the local image store keeps the image) and, when a
    digest for *repository* is on file, confirms it is still live in the
    registry via :func:`agent_image_is_pushed` before trusting it. Only when
    neither check succeeds does this actually push, via
    :func:`push_agent_image`, and it re-verifies the result before returning
    so a caller never receives a digest this function has not confirmed is
    fetchable.

    Raises:
        ImagePushError: if pushing fails, or if the registry does not report
            the freshly pushed digest as present. The message names the
            digest that could not be verified.
    """
    runner = command_runner or SubprocessDockerBuildRunner()
    cached = _cached_repo_digest(image_id, repository, runner=runner, timeout=timeout)
    if cached is not None and agent_image_is_pushed(cached, command_runner=runner, timeout=timeout):
        return cached

    pushed = push_agent_image(
        image_id, repository=repository, command_runner=runner, timeout=timeout
    )
    if not agent_image_is_pushed(pushed, command_runner=runner, timeout=timeout):
        raise ImagePushError(  # noqa: TRY003
            f"Pushed {pushed} but the registry did not report it as present "
            "immediately afterward; retry, or check registry availability."
        )
    return pushed


def _cached_repo_digest(
    image_id: str,
    repository: str,
    *,
    runner: DockerBuildRunner,
    timeout: float,
) -> str | None:
    """Return a previously pushed ``repository@sha256:...`` digest for *image_id*, if known.

    Reads *image_id*'s ``RepoDigests`` from the local Docker image store,
    which Docker updates in place on every successful ``docker push`` of a
    tag pointing at that image ID. Absent, malformed, or Docker-unreachable
    output all resolve to "unknown" rather than raising: the caller falls
    back to actually pushing, which reports the real failure if one exists.
    """
    try:
        result = runner.run(
            ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id),
            cwd=Path.cwd(),
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        digests = json.loads(result.stdout.strip() or "null")
    except json.JSONDecodeError:
        return None
    if not isinstance(digests, list):
        return None
    prefix = f"{repository}@"
    return next(
        (entry for entry in digests if isinstance(entry, str) and entry.startswith(prefix)),
        None,
    )


def _run_registry_command(
    runner: DockerBuildRunner,
    argv: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    action: str,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner.run(argv, cwd=cwd, timeout=timeout)
    except FileNotFoundError as exc:
        raise ImagePushError(f"Docker was not found while {action} the agent image") from exc  # noqa: TRY003
    except subprocess.TimeoutExpired as exc:
        raise ImagePushError(  # noqa: TRY003
            f"Docker timed out after {timeout:g} seconds while {action} the agent image"
        ) from exc
