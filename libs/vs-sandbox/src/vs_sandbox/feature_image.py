"""Build an image by applying Dev Container Features to a base image.

The mechanism knows nothing about what a Feature installs. A caller passes a
base image and an ordered list of Feature folders, plus the user and working
directory the final image should start in. :mod:`vs_sandbox.feature_dockerfile`
turns that into a Dockerfile, and :func:`build_feature_image` runs plain
``docker build`` on it. No container drives the host's Docker daemon through
its socket and no Node.js is needed, so it works on hosts whose daemon refuses
to bind-mount the socket. See ``docs/contributing/devcontainer-features.md``.

Features belong to their owners (the agent layer, a run environment, the
toolchain catalog in :mod:`vs_sandbox.toolchain_features`) and arrive here as
data; this module never imports an owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.feature_dockerfile import (
    BASE_IMAGE_ARG,
    BuildPlan,
    FeatureInstall,
    FeatureSpec,
    FeatureSpecError,
    parse_feature_metadata,
    plan_feature_build,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

_DIAGNOSTIC_LIMIT = 1000
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_METADATA_FILE = "devcontainer-feature.json"


class FeatureImageBuildError(RuntimeError):
    """Raised when the Feature image cannot be built or its ID cannot be resolved."""


@dataclass(frozen=True)
class LocalFeature:
    """A Feature in a local folder holding ``devcontainer-feature.json`` and ``install.sh``.

    The folder's name is the Feature's id and must be unique within one build.
    """

    directory: Path
    options: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def feature_id(self) -> str:
        """The Feature's id, which is its folder name."""
        return self.directory.name


#: A Feature the build accepts. Only local folders are supported; an OCI reference
#: (a Feature published to a registry) is not, because the build runs no tool that
#: fetches one. Add a variant here, with its own fetch step, when one is needed.
type FeatureRef = LocalFeature


class CommandOutcome(Protocol):
    """What a finished Docker command reports (a completed-process object fits)."""

    @property
    def returncode(self) -> int:
        """The exit status."""
        ...

    @property
    def stdout(self) -> str:
        """Captured standard output."""
        ...

    @property
    def stderr(self) -> str:
        """Captured standard error."""
        ...


class DockerCommandRunner(Protocol):
    """Injectable process boundary for the Docker commands of one image build.

    ``run`` raises ``FileNotFoundError`` when Docker is not installed and
    ``TimeoutError`` when the command outlives *timeout*; anything else a
    command does is reported through its outcome.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
    ) -> CommandOutcome:
        """Run one Docker command without a shell."""
        ...


@dataclass(frozen=True)
class FinalImage:
    """The image to produce, and existing images whose layers it may reuse.

    ``cache_from`` names Docker images with inline build-cache metadata. Cache
    sources affect reuse only, never the resulting image's input-addressed tag.
    """

    user: str
    workdir: str
    image_repository: str
    cache_from: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Reject malformed cache sources before issuing any Docker command."""
        if any(
            not source or source.startswith("-") or any(c.isspace() for c in source)
            for source in self.cache_from
        ):
            message = "cache_from must contain nonempty Docker image references without whitespace"
            raise ValueError(message)


@dataclass(frozen=True)
class FeatureImage:
    """A built image: its local tag, derived from the build inputs, and its immutable ID."""

    tag: str
    image_id: str


def _load_spec(feature: LocalFeature) -> FeatureSpec:
    directory = feature.directory
    metadata_path = directory / _METADATA_FILE
    if not metadata_path.is_file() or not (directory / "install.sh").is_file():
        message = f"{directory} is not a Feature: it needs devcontainer-feature.json and install.sh"
        raise FeatureImageBuildError(message)
    try:
        return parse_feature_metadata(
            metadata_path.read_text(encoding="utf-8"), folder_name=directory.name
        )
    except (OSError, FeatureSpecError) as exc:
        raise FeatureImageBuildError(str(exc)) from exc


def feature_project_problems(project: Path) -> list[str]:
    """Return what is wrong with a ``devcontainer features test`` project folder, if anything.

    The folder holds ``src/<id>/`` (metadata and ``install.sh``) and
    ``test/<id>/test.sh`` for each Feature. Checked: the metadata parses and
    names its folder and uses only known keys; scripts are executable; every
    option has a default and is read by the script (as the upper-cased name);
    every Feature has a test script; and no script names a sibling Feature it
    did not declare through ``installsAfter`` or ``dependsOn``, because a
    Feature must install everything it uses.
    """
    problems: list[str] = []
    directories = sorted(path for path in (project / "src").iterdir() if path.is_dir())
    ids = {path.name for path in directories}
    for directory in directories:
        name = directory.name
        try:
            metadata = json.loads((directory / "devcontainer-feature.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            problems.append(f"{name}: unreadable devcontainer-feature.json: {exc}")
            continue
        try:
            parse_feature_metadata(
                (directory / _METADATA_FILE).read_text("utf-8"), folder_name=name
            )
        except FeatureSpecError as exc:
            problems.append(str(exc))
        script_path = directory / "install.sh"
        script = script_path.read_text("utf-8") if script_path.is_file() else ""
        if not script_path.is_file() or not script_path.stat().st_mode & 0o111:
            problems.append(f"{name}: install.sh is missing or not executable")
        for option, spec in metadata.get("options", {}).items():
            if "default" not in spec:
                problems.append(f"{name}: option {option} has no default")
            if option.upper() not in script:
                problems.append(f"{name}: install.sh never reads option {option}")
        declared = set(metadata.get("installsAfter", [])) | set(metadata.get("dependsOn", []))
        problems.extend(
            f"{name}: install.sh names sibling Feature {other} without declaring it"
            for other in sorted(ids - {name} - declared)
            if other in script
        )
        test_script = project / "test" / name / "test.sh"
        if not test_script.is_file() or not test_script.stat().st_mode & 0o111:
            problems.append(f"{name}: test/{name}/test.sh is missing or not executable")
    return problems


def build_feature_image(
    base_image: str,
    features: Sequence[FeatureRef],
    final: FinalImage,
    *,
    command_runner: DockerCommandRunner,
    timeout: float,
) -> FeatureImage:
    """Build *base_image* plus *features*, finished as *final*, and return it.

    One ``docker build`` runs the Dockerfile that
    :func:`~vs_sandbox.feature_dockerfile.plan_feature_build` generates. The
    image has one local tag under ``final.image_repository``, derived from the
    build inputs (the base image, the ordered Features with their options and
    files, the user, and the working directory), so rebuilding the same
    selection moves the same tag instead of adding another. Different inputs
    get different tags. When the daemon already has that tag, nothing is built:
    the tag names every input, so the image is the result, and a build would
    only re-send the context and re-export the image. A base named by a mutable
    registry tag is an input known by name only, so it is the caller's to pin. The image is identified by its immutable ID, which is
    content-addressed: the same inputs on the same base give the same ID from
    Docker's layer cache, whatever the temporary build directory is called.

    Raises:
        FeatureImageBuildError: Docker is missing, a command fails or times out,
            or a Feature is malformed or uses an unsupported metadata field.
        ValueError: *timeout* is not positive.
    """
    if timeout <= 0:
        message = "image build timeout must be positive"
        raise ValueError(message)
    installs = [
        FeatureInstall(_load_spec(feature), feature.directory, feature.options)
        for feature in features
    ]
    try:
        plan = plan_feature_build(installs, user=final.user, workdir=final.workdir)
    except FeatureSpecError as exc:
        raise FeatureImageBuildError(str(exc)) from exc
    run = _Docker(command_runner, timeout)
    with tempfile.TemporaryDirectory(
        prefix="vibesys-feature-image-", ignore_cleanup_errors=True
    ) as tmp:
        context = Path(tmp).resolve()
        for destination, source in plan.copied_folders.items():
            shutil.copytree(source, context / destination, symlinks=True)
        for relative, text in plan.generated_files.items():
            (context / relative).write_text(text, encoding="utf-8")
        dockerfile = context / "Dockerfile"
        dockerfile.write_text(plan.dockerfile, encoding="utf-8")
        base_identity = _base_identity(run, base_image, cwd=context)
        tag = f"{final.image_repository}:{_input_digest(base_identity, plan, context)}"
        present = run.attempt(_inspect_argv(tag), cwd=context)
        if present.returncode == 0:
            # The tag names every input, so this image already is the build's result.
            inspected = present
        else:
            run.checked(
                (
                    "docker",
                    "build",
                    "--provenance=false",
                    "--tag",
                    tag,
                    "--file",
                    str(dockerfile),
                    "--build-arg",
                    f"{BASE_IMAGE_ARG}={base_image}",
                    "--build-arg",
                    "BUILDKIT_INLINE_CACHE=1",
                    *(arg for source in final.cache_from for arg in ("--cache-from", source)),
                    str(context),
                ),
                cwd=context,
                what="feature image build",
            )
            inspected = run.checked(_inspect_argv(tag), cwd=context, what="image inspect")
    image_id = inspected.stdout.strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        message = f"Docker returned an invalid image ID for {tag}: {image_id[:_DIAGNOSTIC_LIMIT]!r}"
        raise FeatureImageBuildError(message)
    return FeatureImage(tag=tag, image_id=image_id)


def _inspect_argv(image: str) -> tuple[str, ...]:
    return ("docker", "image", "inspect", "--format", "{{.Id}}", image)


def _base_identity(run: _Docker, base_image: str, *, cwd: Path) -> str:
    """Return the base image's ID when Docker has it locally, else its name.

    A base that is a freshly built local tag (a task image) changes name on every
    build while its ID stays the same; keying on the ID keeps the result's tag
    stable. A base that is not local (a registry name) keys on its name.
    """
    result = run.attempt(_inspect_argv(base_image), cwd=cwd)
    image_id = result.stdout.strip()
    if result.returncode == 0 and _IMAGE_ID.fullmatch(image_id) is not None:
        return image_id
    return base_image


def _input_digest(base_identity: str, plan: BuildPlan, context: Path) -> str:
    """Hash everything that determines the built image into a 32-character tag."""
    digest = hashlib.sha256()
    for part in (base_identity, plan.dockerfile, *sorted(plan.generated_files.items())):
        digest.update(repr(part).encode())
        digest.update(b"\0")
    paths = (path.relative_to(context).as_posix() for path in context.rglob("*"))
    digest.update(image_context_digest(context, paths).encode())
    return digest.hexdigest()[:32]


class _Docker:
    """Runs Docker commands through the injected runner and names failures."""

    def __init__(self, runner: DockerCommandRunner, timeout: float) -> None:
        self._runner = runner
        self._timeout = timeout

    def attempt(self, argv: Sequence[str], *, cwd: Path) -> CommandOutcome:
        """Run *argv*, mapping a missing Docker or a timeout to a build error."""
        try:
            return self._runner.run(argv, cwd=cwd, timeout=self._timeout)
        except FileNotFoundError as exc:
            message = f"Docker was not found while running {' '.join(argv[:3])}"
            raise FeatureImageBuildError(message) from exc
        except TimeoutError as exc:
            message = f"Docker timed out after {self._timeout:g} seconds: {' '.join(argv[:3])}"
            if str(exc):
                message = f"{message}\n{exc}"
            raise FeatureImageBuildError(message) from exc

    def checked(self, argv: Sequence[str], *, cwd: Path, what: str) -> CommandOutcome:
        """Run *argv* and raise, naming *what* failed, on a nonzero exit."""
        result = self.attempt(argv, cwd=cwd)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"{what} failed").strip()
            # The tail: a build prints the failing step last.
            message = f"{what} failed (exit {result.returncode}): {detail[-_DIAGNOSTIC_LIMIT:]}"
            raise FeatureImageBuildError(message)
        return result


def image_context_digest(context: Path, relative_paths: Iterable[str]) -> str:
    """Hash Docker context entries, including links, permissions and Linux xattrs.

    Callers select entries after their context's ignore policy. Hard-link groups
    use their first relative path, so inode numbers never enter the identity.
    Modification times are excluded consistently with Docker's cache checksum.
    Python stages extended attributes on Linux; other supported platforms do not
    expose those filesystem attributes through Python's copy2 implementation.
    """
    digest = hashlib.sha256()
    hardlinks: dict[tuple[int, int], str] = {}
    for relative in sorted(relative_paths):
        path = context / relative
        metadata = path.lstat()
        mode = metadata.st_mode
        link = ""
        if stat.S_ISLNK(mode):
            payload = os.fsencode(path.readlink())
        elif stat.S_ISREG(mode):
            link = hardlinks.setdefault((metadata.st_dev, metadata.st_ino), relative)
            payload = path.read_bytes()
        elif stat.S_ISDIR(mode):
            payload = b""
        else:
            message = f"unsupported image input: {path}"
            raise ValueError(message)
        parts = (
            os.fsencode(relative),
            str(mode).encode(),
            str(metadata.st_uid).encode(),
            str(metadata.st_gid).encode(),
            os.fsencode(link),
            payload,
            *_image_xattrs(path),
        )
        digest.update(len(parts).to_bytes(8, "big"))
        for part in parts:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


def _image_xattrs(path: Path) -> tuple[bytes, ...]:
    if sys.platform != "linux":
        return ()
    return tuple(
        part
        for name in sorted(os.listxattr(path, follow_symlinks=False))
        for part in (os.fsencode(name), os.getxattr(path, name, follow_symlinks=False))
    )
