"""Resolve immutable evaluator packages from a local package collection.

The resolver owns the evaluator package filesystem contract. Callers deal in
validated requirements and resolved packages, without depending on metadata
file names, resource layout, or content-digest implementation details.
"""

from __future__ import annotations

import hashlib
import re
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vibesys.resource_paths import evaluator_packages_dir

if TYPE_CHECKING:
    from pathlib import Path

EVALUATOR_PACKAGE_METADATA_NAME = "vibesys.evaluator.toml"
PACKAGE_ROOT_TOKEN = "${PACKAGE_ROOT}"  # noqa: S105  # lint-waiver: LW-007086 [S105]; Public argv template token, not a credential.
PROJECT_ROOT_TOKEN = "${PROJECT_ROOT}"  # noqa: S105  # lint-waiver: LW-007087 [S105]; Public argv template token, not a credential.
PYTHON_TOKEN = "${PYTHON}"  # noqa: S105  # lint-waiver: LW-007088 [S105]; Public argv template token, not a credential.
TOOL_TOKEN_PREFIX = "${TOOL:"  # noqa: S105  # lint-waiver: LW-007089 [S105]; Public argv template token, not a credential.

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_CARGO_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_TOOL_TOKEN_PATTERN = re.compile(
    r"^\$\{TOOL:(?P<tool>[a-z0-9]+(?:[.-][a-z0-9]+)*)/"
    r"(?P<binary>[a-z0-9]+(?:[-_][a-z0-9]+)*)\}$"
)
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*$")
_DIGEST_EXCLUDED_NAMES = frozenset({".git", "__pycache__", "target"})


class EvaluatorPackageError(ValueError):
    """Base error for invalid or ambiguous evaluator packages."""

    @classmethod
    def missing_entrypoint(
        cls, package: str, entrypoint: str, available: str
    ) -> EvaluatorPackageError:
        """Describe an entrypoint absent from a resolved package."""
        return cls(
            f"evaluator package {package!r} has no entrypoint {entrypoint!r}; "
            f"available entrypoints: {available}"
        )

    @classmethod
    def duplicate_package(cls, name: str, version: str, locations: str) -> EvaluatorPackageError:
        """Describe multiple local packages matching one exact requirement."""
        return cls(f"duplicate evaluator package {name}=={version}: {locations}")

    @classmethod
    def invalid_directory(cls, path: Path) -> EvaluatorPackageError:
        """Describe a package root that is not a directory."""
        return cls(f"evaluator package is not a directory: {path}")

    @classmethod
    def missing_metadata(cls, path: Path) -> EvaluatorPackageError:
        """Describe a package directory without its metadata file."""
        return cls(f"evaluator package metadata not found: {path}")

    @classmethod
    def invalid_metadata(cls, path: Path, error: Exception) -> EvaluatorPackageError:
        """Describe package metadata that failed parsing or validation."""
        return cls(f"invalid evaluator package metadata {path}: {error}")

    @classmethod
    def contains_symlink(cls, path: Path) -> EvaluatorPackageError:
        """Describe a package containing a symbolic link."""
        return cls(f"evaluator packages may not contain symlinks: {path}")


class EvaluatorPackageNotFoundError(EvaluatorPackageError):
    """Raised when a local collection cannot satisfy an exact requirement."""

    @classmethod
    def missing_collection(cls, path: Path) -> EvaluatorPackageNotFoundError:
        """Describe a package collection that does not exist."""
        return cls(f"evaluator package collection does not exist: {path}")

    @classmethod
    def missing_requirement(
        cls, name: str, version: str, path: Path, available: str
    ) -> EvaluatorPackageNotFoundError:
        """Describe an exact package version absent from a collection."""
        detail = f"; available packages: {available}" if available else ""
        return cls(f"evaluator package {name}=={version} not found in {path}{detail}")

    @classmethod
    def resources_unavailable(cls) -> EvaluatorPackageNotFoundError:
        """Describe a distribution without bundled evaluator package resources."""
        return cls(
            "VibeSys evaluator package resources are not available; install a complete "
            "VibeSys distribution or pass packages_root"
        )


class CargoGitToolSpec(BaseModel):
    """One Cargo package installed from an immutable Git revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cargo-git"]
    git: str
    rev: str
    package: str
    bins: tuple[str, ...]

    @field_validator("git")
    @classmethod
    def _valid_git_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            message = "git must be an HTTPS URL without credentials, query, or fragment"
            raise ValueError(message)
        return value

    @field_validator("rev")
    @classmethod
    def _full_git_revision(cls, value: str) -> str:
        if not _GIT_REVISION_PATTERN.fullmatch(value):
            message = "rev must be a full 40-character lowercase Git commit SHA"
            raise ValueError(message)
        return value

    @field_validator("package")
    @classmethod
    def _valid_package(cls, value: str) -> str:
        if not _CARGO_IDENTIFIER_PATTERN.fullmatch(value):
            message = "package must be a canonical Cargo package name"
            raise ValueError(message)
        return value

    @field_validator("bins")
    @classmethod
    def _valid_bins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            message = "bins must declare at least one binary"
            raise ValueError(message)
        if len(value) != len(set(value)):
            message = "bins must not contain duplicates"
            raise ValueError(message)
        invalid = next(
            (binary for binary in value if not _CARGO_IDENTIFIER_PATTERN.fullmatch(binary)),
            None,
        )
        if invalid is not None:
            message = f"invalid Cargo binary name: {invalid!r}"
            raise ValueError(message)
        return value


class EvaluatorPackageRequirement(BaseModel):
    """An exact evaluator package version requested by a task."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    version: str

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not _IDENTIFIER_PATTERN.fullmatch(value):
            message = "name must contain lowercase letters and digits separated by '-' or '.'"
            raise ValueError(message)
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if not _VERSION_PATTERN.fullmatch(value):
            message = "version must be an exact package version without whitespace"
            raise ValueError(message)
        return value


class EvaluatorPackageMetadata(EvaluatorPackageRequirement):
    """Validated contents of ``vibesys.evaluator.toml``."""

    schema_version: Literal[1]
    protocol_version: Literal[1]
    toolchains: tuple[Literal["go", "rust"], ...] = ()
    tools: dict[str, CargoGitToolSpec] = Field(default_factory=dict)
    entrypoints: dict[str, tuple[str, ...]]

    @field_validator("toolchains")
    @classmethod
    def _unique_toolchains(
        cls,
        value: tuple[Literal["go", "rust"], ...],
    ) -> tuple[Literal["go", "rust"], ...]:
        if len(value) != len(set(value)):
            message = "toolchains must not contain duplicates"
            raise ValueError(message)
        return value

    @field_validator("entrypoints")
    @classmethod
    def _valid_entrypoints(
        cls,
        value: dict[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]]:
        if not value:
            message = "entrypoints must define at least one command"
            raise ValueError(message)
        for name, command in value.items():
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                message = (
                    f"entrypoint {name!r} must contain lowercase letters and digits "
                    "separated by '-' or '.'"
                )
                raise ValueError(message)
            if not command:
                message = f"entrypoint {name!r} must contain at least one argv element"
                raise ValueError(message)
            if any(not part for part in command):
                message = f"entrypoint {name!r} contains an empty argv element"
                raise ValueError(message)
            if any(PYTHON_TOKEN in part and part != PYTHON_TOKEN for part in command):
                message = (
                    f"entrypoint {name!r} contains a malformed Python token; "
                    "${PYTHON} must occupy one complete argv element"
                )
                raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _valid_tools(self) -> EvaluatorPackageMetadata:
        for name in self.tools:
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                message = f"invalid evaluator tool name: {name!r}"
                raise ValueError(message)
        for entrypoint, command in self.entrypoints.items():
            _validate_tool_tokens(self.tools, command, location=f"entrypoint {entrypoint!r}")
        return self


def tool_token(tool: str, binary: str) -> str:
    """Return the semantic argv token for one declared evaluator tool binary."""
    return f"${{TOOL:{tool}/{binary}}}"


def _validate_tool_tokens(
    tools: dict[str, CargoGitToolSpec], command: tuple[str, ...], *, location: str
) -> None:
    for part in command:
        match = _TOOL_TOKEN_PATTERN.fullmatch(part)
        if match is None:
            if TOOL_TOKEN_PREFIX in part:
                message = (
                    f"{location} contains a malformed tool token; "
                    "tool tokens must occupy one complete argv element"
                )
                raise ValueError(message)
            continue
        tool_name = match.group("tool")
        binary = match.group("binary")
        try:
            tool = tools[tool_name]
        except KeyError as exc:
            message = f"{location} references undeclared tool {tool_name!r}"
            raise ValueError(message) from exc
        if binary not in tool.bins:
            message = f"{location} references undeclared binary {binary!r} from tool {tool_name!r}"
            raise ValueError(message)


@dataclass(frozen=True)
class ResolvedEvaluatorPackage:
    """One validated local evaluator package pinned by its content digest."""

    root: Path
    metadata: EvaluatorPackageMetadata
    digest: str

    @property
    def name(self) -> str:
        """Return the published package name."""
        return self.metadata.name

    @property
    def version(self) -> str:
        """Return the exact published package version."""
        return self.metadata.version

    def command(
        self,
        entrypoint: str,
        *arguments: str,
        project_root: Path | None = None,
    ) -> tuple[str, ...]:
        """Return an argv sequence for ``entrypoint`` plus task arguments.

        Local source packages use ``${PACKAGE_ROOT}`` in their metadata to
        remain independent of the candidate process's working directory.
        Published packages may instead map the same entrypoint to an installed
        executable on ``PATH``. Run-environment adapters supply ``project_root``
        when they need to expand ``${PROJECT_ROOT}`` in task arguments.
        """
        try:
            command = self.metadata.entrypoints[entrypoint]
        except KeyError as exc:
            available = ", ".join(sorted(self.metadata.entrypoints))
            raise EvaluatorPackageError.missing_entrypoint(
                self.name, entrypoint, available
            ) from exc
        try:
            _validate_tool_tokens(
                self.metadata.tools,
                tuple(arguments),
                location=f"arguments for entrypoint {entrypoint!r}",
            )
        except ValueError as exc:
            raise EvaluatorPackageError(str(exc)) from exc
        package_root = str(self.root)
        resolved_command = tuple(part.replace(PACKAGE_ROOT_TOKEN, package_root) for part in command)
        resolved_arguments = tuple(
            part.replace(PACKAGE_ROOT_TOKEN, package_root) for part in arguments
        )
        if project_root is None:
            return resolved_command + resolved_arguments
        candidate_root = str(project_root)
        return resolved_command + tuple(
            part.replace(PROJECT_ROOT_TOKEN, candidate_root) for part in resolved_arguments
        )


class EvaluatorPackageRegistry:
    """Resolve exact evaluator versions from one local package collection."""

    def __init__(self, root: Path) -> None:
        """Create a registry rooted at a local package collection."""
        self.root = root.expanduser().resolve()

    def resolve(self, requirement: EvaluatorPackageRequirement) -> ResolvedEvaluatorPackage:
        """Resolve one exact package requirement or raise a diagnostic error."""
        if not self.root.is_dir():
            raise EvaluatorPackageNotFoundError.missing_collection(self.root)

        packages = self._packages()
        matches = [
            package
            for package in packages
            if package.name == requirement.name and package.version == requirement.version
        ]
        if not matches:
            available = sorted(f"{package.name}=={package.version}" for package in packages)
            raise EvaluatorPackageNotFoundError.missing_requirement(
                requirement.name, requirement.version, self.root, ", ".join(available)
            )
        if len(matches) > 1:
            locations = ", ".join(str(package.root) for package in matches)
            raise EvaluatorPackageError.duplicate_package(
                requirement.name, requirement.version, locations
            )
        return matches[0]

    def _packages(self) -> tuple[ResolvedEvaluatorPackage, ...]:
        return tuple(
            load_evaluator_package(child)
            for child in sorted(self.root.iterdir(), key=lambda path: path.name)
            if child.is_dir() and (child / EVALUATOR_PACKAGE_METADATA_NAME).is_file()
        )


def load_evaluator_package(path: Path) -> ResolvedEvaluatorPackage:
    """Load one self-contained evaluator package directory."""
    root = path.expanduser().resolve()
    metadata_path = root / EVALUATOR_PACKAGE_METADATA_NAME
    if not root.is_dir():
        raise EvaluatorPackageError.invalid_directory(root)
    if not metadata_path.is_file():
        raise EvaluatorPackageError.missing_metadata(metadata_path)
    try:
        document = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = EvaluatorPackageMetadata.model_validate(document)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise EvaluatorPackageError.invalid_metadata(metadata_path, exc) from exc
    return ResolvedEvaluatorPackage(
        root=root,
        metadata=metadata,
        digest=_content_digest(root),
    )


def resolve_evaluator_package(
    requirement: EvaluatorPackageRequirement,
    *,
    packages_root: Path | None = None,
) -> ResolvedEvaluatorPackage:
    """Resolve a package from an explicit collection or VibeSys resources."""
    root = packages_root if packages_root is not None else evaluator_packages_dir()
    if root is None:
        raise EvaluatorPackageNotFoundError.resources_unavailable()
    return EvaluatorPackageRegistry(root).resolve(requirement)


def _content_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"), key=lambda candidate: candidate.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root)
        if any(part in _DIGEST_EXCLUDED_NAMES for part in relative.parts):
            continue
        if path.is_symlink():
            raise EvaluatorPackageError.contains_symlink(path)
        if not path.is_file() or path.suffix == ".pyc":
            continue
        relative_bytes = relative.as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(8, "big"))
        digest.update(relative_bytes)
        digest.update((path.stat().st_mode & 0o111).to_bytes(2, "big"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return f"sha256:{digest.hexdigest()}"
