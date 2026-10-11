"""Build an image by applying Dev Container Features to a base image.

The mechanism knows nothing about what a Feature installs. A caller passes a
base image and an ordered list of Feature references (a local folder, or an OCI
reference), plus the user and working directory the final image should start
in. :func:`feature_config` turns that into a ``devcontainer.json``, and
:func:`build_feature_image` runs the pinned devcontainer CLI, in a container
that drives the host's Docker daemon through its socket, so a host needs only
Docker.

Features belong to their owners (the agent layer, a run environment, the
toolchain catalog in :mod:`vs_sandbox.toolchain_features`) and arrive here as
data; this module never imports an owner.

:func:`feature_config` is pure and deterministic: the same inputs give the
same document, so the build cache key depends only on what was selected.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

_DIAGNOSTIC_LIMIT = 1000
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_SHORT_ID_LENGTH = 12
_NAME = re.compile(r"[a-z_][a-z0-9_-]*")
_FEATURES_REFERENCE_DIR = "features"
_FEATURES_STAGE_REPOSITORY = "vibesys-features-build"
_DEVCONTAINER_CLI_REPOSITORY = "vibesys-devcontainer-cli"

#: The Docker socket the containerized CLI drives. It is root-equivalent on the
#: host, the same trust Docker access already grants.
DOCKER_SOCKET = "/var/run/docker.sock"

#: The Dockerfile of the image that runs the devcontainer CLI. Resolved relative
#: to this module so the path is a real filesystem path Docker can build from,
#: in a source checkout or an installed wheel alike.
DEVCONTAINER_CLI_DOCKERFILE = Path(__file__).resolve().parent / "devcontainer-cli" / "Dockerfile"


class DevcontainerConfig(TypedDict):
    """The ``devcontainer.json`` document :func:`feature_config` produces.

    The key names are fixed by the devcontainer specification.
    """

    image: str
    features: dict[str, dict[str, str]]
    overrideFeatureInstallOrder: list[str]
    containerUser: str


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

    @property
    def key(self) -> str:
        """The key under ``features`` in ``devcontainer.json``."""
        return f"./{_FEATURES_REFERENCE_DIR}/{self.feature_id}"


@dataclass(frozen=True)
class OciFeature:
    """A Feature published to an OCI registry, such as ``ghcr.io/org/features/x:1``."""

    reference: str
    options: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def feature_id(self) -> str:
        """The Feature's id, which is its reference."""
        return self.reference

    @property
    def key(self) -> str:
        """The key under ``features`` in ``devcontainer.json``."""
        return self.reference


type FeatureRef = LocalFeature | OciFeature


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
    """How the finished image starts and what its local tag is called."""

    user: str
    workdir: str
    image_repository: str


@dataclass(frozen=True)
class FeatureImage:
    """A built image: its unique local tag and its immutable ID."""

    tag: str
    image_id: str


def feature_config(
    base_image: str, features: Sequence[FeatureRef], *, user: str
) -> DevcontainerConfig:
    """Return the ``devcontainer.json`` that applies *features* to *base_image*, in order.

    ``overrideFeatureInstallOrder`` pins the given order: without it the CLI
    orders Features alphabetically, so a caller could not put the Feature that
    changes least first and keep later rebuilds small.

    Raises:
        ValueError: Two Features share an id, or *user* is not a plain user name.
    """
    if _NAME.fullmatch(user) is None:
        message = f"user must be a plain user name: {user!r}"
        raise ValueError(message)
    keys = [feature.key for feature in features]
    duplicates = sorted({key for key in keys if keys.count(key) > 1})
    if duplicates:
        message = f"Feature listed twice: {duplicates!r}"
        raise ValueError(message)
    return {
        "image": base_image,
        "features": {feature.key: dict(feature.options) for feature in features},
        "overrideFeatureInstallOrder": keys,
        "containerUser": user,
    }


def finish_dockerfile(*, user: str, workdir: str) -> str:
    """Return the stage that makes the CLI's image start as *user* in *workdir*.

    The CLI keeps the base image's user and working directory and records
    ``containerUser`` only in a metadata label, so the image needs this last
    stage to run as that user without flags at run time.
    """
    if not workdir.startswith("/") or any(ch.isspace() for ch in workdir):
        message = f"workdir must be an absolute path without whitespace: {workdir!r}"
        raise ValueError(message)
    return f"ARG BASE_IMAGE\nFROM ${{BASE_IMAGE}}\nWORKDIR {workdir}\nUSER {user}\n"


def validate_feature_directory(directory: Path, options: Mapping[str, str]) -> None:
    """Fail unless *directory* is a Feature that declares every option in *options*.

    Raises:
        FeatureImageBuildError: The folder lacks a metadata file or script, or
            an option is not declared by the Feature.
    """
    metadata_path = directory / "devcontainer-feature.json"
    if not metadata_path.is_file() or not (directory / "install.sh").is_file():
        message = f"{directory} is not a Feature: it needs devcontainer-feature.json and install.sh"
        raise FeatureImageBuildError(message)
    declared = json.loads(metadata_path.read_text(encoding="utf-8")).get("options", {})
    unknown = sorted(set(options) - set(declared))
    if unknown:
        message = f"Feature {directory.name} declares no option {unknown!r}"
        raise FeatureImageBuildError(message)


_ALLOWED_METADATA_KEYS = frozenset(
    {
        "id", "version", "name", "description", "documentationURL", "options",
        "containerEnv", "installsAfter", "dependsOn",
    }
)  # fmt: skip


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
        if metadata.get("id") != name:
            problems.append(f"{name}: metadata id {metadata.get('id')!r} differs from the folder")
        problems.extend(
            f"{name}: unknown metadata key {key!r}"
            for key in set(metadata) - _ALLOWED_METADATA_KEYS
        )
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

    The image has a unique local tag under ``final.image_repository`` and is identified
    by its immutable ID, which is content-addressed: the same inputs on the same
    base give the same ID from Docker's layer cache. The intermediate image the
    CLI produces is untagged again once the final one exists.

    The CLI container sees the workspace at the same absolute path as the host,
    because the CLI hands that path to the host's Docker daemon as a build
    context.

    Raises:
        FeatureImageBuildError: Docker is missing, a command fails or times out,
            or a local Feature is malformed.
        ValueError: *timeout* is not positive, or the inputs are invalid
            (see :func:`feature_config`).
    """
    if timeout <= 0:
        message = "image build timeout must be positive"
        raise ValueError(message)
    config = feature_config(base_image, features, user=final.user)
    finish = finish_dockerfile(user=final.user, workdir=final.workdir)
    for feature in features:
        if isinstance(feature, LocalFeature):
            validate_feature_directory(feature.directory, feature.options)
    run = _Docker(command_runner, timeout)
    cli_image = _devcontainer_cli_image(run)
    with tempfile.TemporaryDirectory(
        prefix="vibesys-feature-image-", ignore_cleanup_errors=True
    ) as tmp:
        root = Path(tmp).resolve()
        _write_workspace(root, config, features, finish)
        features_tag = f"{_FEATURES_STAGE_REPOSITORY}:{uuid.uuid4().hex}"
        run.checked(
            (
                "docker",
                "run",
                "--rm",
                "--volume",
                f"{DOCKER_SOCKET}:{DOCKER_SOCKET}",
                "--volume",
                f"{root}:{root}",
                cli_image,
                "build",
                "--workspace-folder",
                str(root),
                "--image-name",
                features_tag,
            ),
            cwd=root,
            what="devcontainer build",
        )
        try:
            return _finish(run, root, features_tag, final.image_repository)
        finally:
            # Only the name: the final image keeps every layer. A leftover
            # stage tag is harmless, so a failure here is ignored.
            run.attempt(("docker", "image", "rm", features_tag), cwd=root)


def _write_workspace(
    root: Path, config: DevcontainerConfig, features: Sequence[FeatureRef], finish: str
) -> None:
    devcontainer_dir = root / ".devcontainer"
    devcontainer_dir.mkdir(parents=True)
    for feature in features:
        if isinstance(feature, LocalFeature):
            shutil.copytree(
                feature.directory, devcontainer_dir / _FEATURES_REFERENCE_DIR / feature.feature_id
            )
    (devcontainer_dir / "devcontainer.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8"
    )
    (root / "finish.Dockerfile").write_text(finish, encoding="utf-8")


def _finish(run: _Docker, root: Path, features_tag: str, image_repository: str) -> FeatureImage:
    tag = f"{image_repository}:{uuid.uuid4().hex}"
    run.checked(
        (
            "docker",
            "build",
            "--provenance=false",
            "--tag",
            tag,
            "--file",
            str(root / "finish.Dockerfile"),
            "--build-arg",
            f"BASE_IMAGE={features_tag}",
            str(root),
        ),
        cwd=root,
        what="finishing build",
    )
    inspected = run.checked(
        ("docker", "image", "inspect", "--format", "{{.Id}}", tag), cwd=root, what="image inspect"
    )
    image_id = inspected.stdout.strip()
    if _IMAGE_ID.fullmatch(image_id) is None:
        message = f"Docker returned an invalid image ID for {tag}: {image_id[:_DIAGNOSTIC_LIMIT]!r}"
        raise FeatureImageBuildError(message)
    return FeatureImage(tag=tag, image_id=image_id)


def _devcontainer_cli_image(run: _Docker) -> str:
    """Build (or find in the layer cache) the image that runs the devcontainer CLI.

    The tag is keyed by the Dockerfile's content, so editing the pinned CLI,
    Node, or Docker client versions yields a new image, and an unchanged one is
    a cache hit that costs about a second.
    """
    digest = hashlib.sha256(DEVCONTAINER_CLI_DOCKERFILE.read_bytes()).hexdigest()
    tag = f"{_DEVCONTAINER_CLI_REPOSITORY}:{digest[:_SHORT_ID_LENGTH]}"
    context = DEVCONTAINER_CLI_DOCKERFILE.parent
    run.checked(
        (
            "docker",
            "build",
            "--provenance=false",
            "--tag",
            tag,
            "--file",
            str(DEVCONTAINER_CLI_DOCKERFILE),
            str(context),
        ),
        cwd=context,
        what="devcontainer CLI image build",
    )
    return tag


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
            raise FeatureImageBuildError(message) from exc

    def checked(self, argv: Sequence[str], *, cwd: Path, what: str) -> CommandOutcome:
        """Run *argv* and raise, naming *what* failed, on a nonzero exit."""
        result = self.attempt(argv, cwd=cwd)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or f"{what} failed").strip()
            message = f"{what} failed (exit {result.returncode}): {detail[:_DIAGNOSTIC_LIMIT]}"
            raise FeatureImageBuildError(message)
        return result
