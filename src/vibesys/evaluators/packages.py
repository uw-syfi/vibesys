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
PACKAGE_ROOT_TOKEN = "${PACKAGE_ROOT}"  # noqa: S105
PROJECT_ROOT_TOKEN = "${PROJECT_ROOT}"  # noqa: S105
PYTHON_TOKEN = "${PYTHON}"  # noqa: S105
TOOL_TOKEN_PREFIX = "${TOOL:"  # noqa: S105

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


class EvaluatorPackageNotFoundError(EvaluatorPackageError):
    """Raised when a local collection cannot satisfy an exact requirement."""


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
            raise ValueError(  # noqa: TRY003
                "git must be an HTTPS URL without credentials, query, or fragment"
            )
        return value

    @field_validator("rev")
    @classmethod
    def _full_git_revision(cls, value: str) -> str:
        if not _GIT_REVISION_PATTERN.fullmatch(value):
            raise ValueError("rev must be a full 40-character lowercase Git commit SHA")  # noqa: TRY003
        return value

    @field_validator("package")
    @classmethod
    def _valid_package(cls, value: str) -> str:
        if not _CARGO_IDENTIFIER_PATTERN.fullmatch(value):
            raise ValueError("package must be a canonical Cargo package name")  # noqa: TRY003
        return value

    @field_validator("bins")
    @classmethod
    def _valid_bins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("bins must declare at least one binary")  # noqa: TRY003
        if len(value) != len(set(value)):
            raise ValueError("bins must not contain duplicates")  # noqa: TRY003
        invalid = next(
            (binary for binary in value if not _CARGO_IDENTIFIER_PATTERN.fullmatch(binary)),
            None,
        )
        if invalid is not None:
            raise ValueError(f"invalid Cargo binary name: {invalid!r}")  # noqa: TRY003
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
            raise ValueError(  # noqa: TRY003
                "name must contain lowercase letters and digits separated by '-' or '.'"
            )
        return value

    @field_validator("version")
    @classmethod
    def _valid_version(cls, value: str) -> str:
        if not _VERSION_PATTERN.fullmatch(value):
            raise ValueError(  # noqa: TRY003
                "version must be an exact package version without whitespace"
            )
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
            raise ValueError("toolchains must not contain duplicates")  # noqa: TRY003
        return value

    @field_validator("entrypoints")
    @classmethod
    def _valid_entrypoints(
        cls,
        value: dict[str, tuple[str, ...]],
    ) -> dict[str, tuple[str, ...]]:
        if not value:
            raise ValueError("entrypoints must define at least one command")  # noqa: TRY003
        for name, command in value.items():
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                raise ValueError(  # noqa: TRY003
                    f"entrypoint {name!r} must contain lowercase letters and digits "
                    "separated by '-' or '.'"
                )
            if not command:
                raise ValueError(  # noqa: TRY003
                    f"entrypoint {name!r} must contain at least one argv element"
                )
            if any(not part for part in command):
                raise ValueError(  # noqa: TRY003
                    f"entrypoint {name!r} contains an empty argv element"
                )
            if any(PYTHON_TOKEN in part and part != PYTHON_TOKEN for part in command):
                raise ValueError(  # noqa: TRY003
                    f"entrypoint {name!r} contains a malformed Python token; "
                    "${PYTHON} must occupy one complete argv element"
                )
        return value

    @model_validator(mode="after")
    def _valid_tools(self) -> EvaluatorPackageMetadata:
        for name in self.tools:
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                raise ValueError(f"invalid evaluator tool name: {name!r}")  # noqa: TRY003
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
                raise ValueError(  # noqa: TRY003
                    f"{location} contains a malformed tool token; "
                    "tool tokens must occupy one complete argv element"
                )
            continue
        tool_name = match.group("tool")
        binary = match.group("binary")
        try:
            tool = tools[tool_name]
        except KeyError as exc:
            raise ValueError(  # noqa: TRY003
                f"{location} references undeclared tool {tool_name!r}"
            ) from exc
        if binary not in tool.bins:
            raise ValueError(  # noqa: TRY003
                f"{location} references undeclared binary {binary!r} from tool {tool_name!r}"
            )


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
            raise EvaluatorPackageError(  # noqa: TRY003
                f"evaluator package {self.name!r} has no entrypoint {entrypoint!r}; "
                f"available entrypoints: {available}"
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
            raise EvaluatorPackageNotFoundError(  # noqa: TRY003
                f"evaluator package collection does not exist: {self.root}"
            )

        packages = self._packages()
        matches = [
            package
            for package in packages
            if package.name == requirement.name and package.version == requirement.version
        ]
        if not matches:
            available = sorted(f"{package.name}=={package.version}" for package in packages)
            detail = f"; available packages: {', '.join(available)}" if available else ""
            raise EvaluatorPackageNotFoundError(  # noqa: TRY003
                f"evaluator package {requirement.name}=={requirement.version} not found "
                f"in {self.root}{detail}"
            )
        if len(matches) > 1:
            locations = ", ".join(str(package.root) for package in matches)
            raise EvaluatorPackageError(  # noqa: TRY003
                f"duplicate evaluator package {requirement.name}=={requirement.version}: "
                f"{locations}"
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
        raise EvaluatorPackageError(  # noqa: TRY003
            f"evaluator package is not a directory: {root}"
        )
    if not metadata_path.is_file():
        raise EvaluatorPackageError(  # noqa: TRY003
            f"evaluator package metadata not found: {metadata_path}"
        )
    try:
        document = tomllib.loads(metadata_path.read_text(encoding="utf-8"))
        metadata = EvaluatorPackageMetadata.model_validate(document)
    except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
        raise EvaluatorPackageError(  # noqa: TRY003
            f"invalid evaluator package metadata {metadata_path}: {exc}"
        ) from exc
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
        raise EvaluatorPackageNotFoundError(  # noqa: TRY003
            "VibeSys evaluator package resources are not available; install a complete "
            "VibeSys distribution or pass packages_root"
        )
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
            raise EvaluatorPackageError(  # noqa: TRY003
                f"evaluator packages may not contain symlinks: {path}"
            )
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
