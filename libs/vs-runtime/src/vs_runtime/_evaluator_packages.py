"""Resolve immutable evaluator packages from a local package collection.

The resolver owns the evaluator package filesystem contract. Callers deal in
validated requirements and resolved packages, without depending on metadata
file names, resource layout, or content-digest implementation details.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vs_runtime._build_outputs import check_candidate_output_path
from vs_sandbox.api.command_translation import PROJECT_ROOT_TOKEN, PYTHON_TOKEN
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec

if TYPE_CHECKING:
    from pathlib import Path

EVALUATOR_PACKAGE_METADATA_NAME = "vibesys.evaluator.toml"
PACKAGE_ROOT_TOKEN = "${PACKAGE_ROOT}"  # noqa: S105  # lint-waiver: LW-007086 [S105]; Public argv template token, not a credential.
TOOL_TOKEN_PREFIX = "${TOOL:"  # noqa: S105  # lint-waiver: LW-007089 [S105]; Public argv template token, not a credential.

_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_TOOL_TOKEN_PATTERN = re.compile(
    r"^\$\{TOOL:(?P<tool>[a-z0-9]+(?:[.-][a-z0-9]+)*)/"
    r"(?P<binary>[a-z0-9]+(?:[-_][a-z0-9]+)*)\}$"
)
_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9]+(?:[._+-][A-Za-z0-9]+)*$")
_ENVIRONMENT_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DIGEST_EXCLUDED_NAMES = frozenset({".git", "__pycache__", "target"})
_ENVIRONMENT_FLAG_OPTIONS = frozenset({"-i", "--ignore-environment", "-0", "--null", "--debug"})
_ENVIRONMENT_VALUE_OPTIONS = frozenset({"-u", "--unset", "-C", "--chdir", "-a", "--argv0"})
_SHELL_NAMES = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_SHELL_CONTROL_TOKENS = frozenset({"&", "&&", "(", ")", ";", "|", "||"})


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

    @classmethod
    def invalid_go_module(cls, path: Path, detail: str) -> EvaluatorPackageError:
        """Describe an incomplete declared Go module inside a package."""
        return cls(f"invalid evaluator Go module {path}: {detail}")


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
    go_module: str | None = None
    tools: dict[str, CargoGitToolSpec] = Field(default_factory=dict)
    entrypoints: dict[str, tuple[str, ...]]
    candidate_outputs: tuple[str, ...] = ()
    """Files a candidate's build must produce, as workspace-relative POSIX paths.

    The evaluator loads them from the checkout it runs in. A task whose evaluator requires
    outputs declares a ``[build]`` that produces them; the framework verifies each one after
    the build and fails the build stage naming the first missing one.
    """

    @field_validator("candidate_outputs")
    @classmethod
    def _valid_candidate_outputs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for output in value:
            check_candidate_output_path(output)
        if len(value) != len(set(value)):
            message = "candidate_outputs must not contain duplicates"
            raise ValueError(message)
        return value

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

    @field_validator("go_module")
    @classmethod
    def _valid_go_module(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or value != path.as_posix()
            or any(part in {"", ".."} for part in path.parts)
        ):
            message = "go_module must be a normalized package-relative POSIX path"
            raise ValueError(message)
        excluded = _DIGEST_EXCLUDED_NAMES.intersection(path.parts)
        if excluded:
            name = min(excluded)
            message = f"go_module cannot use digest-excluded path component {name!r}"
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
        if self.go_module is not None and "go" not in self.toolchains:
            message = "go_module requires the 'go' toolchain"
            raise ValueError(message)
        for name in self.tools:
            if not _IDENTIFIER_PATTERN.fullmatch(name):
                message = f"invalid evaluator tool name: {name!r}"
                raise ValueError(message)
        for entrypoint, command in self.entrypoints.items():
            _validate_tool_tokens(self.tools, command, location=f"entrypoint {entrypoint!r}")
            if _may_invoke_go_run(command) and self.go_module is None:
                message = f"entrypoint {entrypoint!r} uses go run but declares no go_module"
                raise ValueError(message)
        return self


def _may_invoke_go_run(command: tuple[str, ...]) -> bool:
    """Identify ``go run`` through the evaluator command's supported argv wrappers."""
    if not command:
        return False
    executable = PurePosixPath(command[0]).name
    arguments = command[1:]
    if executable == "go":
        return _go_command_runs(arguments)
    if executable == "env":
        nested = _environment_command(arguments)
        return nested is not None and _may_invoke_go_run(nested)
    if executable == "command":
        nested = _command_builtin_command(arguments)
        return nested is not None and _may_invoke_go_run(nested)
    if executable in _SHELL_NAMES:
        return _shell_command_runs(arguments)
    return False


def _go_command_runs(arguments: tuple[str, ...]) -> bool:
    """Return whether a direct Go argv selects the ``run`` subcommand."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "-C":
            index += 2
            continue
        if argument.startswith("-C="):
            index += 1
            continue
        return argument == "run"
    return False


def _environment_command(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the command operand of a supported ``env`` invocation."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return arguments[index + 1 :] or None
        split = _environment_split_command(arguments, index)
        if split is not None:
            return split or None
        option_width = _environment_option_width(argument)
        if option_width is not None:
            index += option_width
            continue
        if _ENVIRONMENT_ASSIGNMENT_PATTERN.match(argument) is not None:
            index += 1
            continue
        if argument.startswith("-"):
            return None
        return arguments[index:]
    return None


def _environment_split_command(arguments: tuple[str, ...], index: int) -> tuple[str, ...] | None:
    """Expand one supported GNU ``env --split-string`` operand."""
    argument = arguments[index]
    if argument in {"-S", "--split-string"}:
        if index + 1 >= len(arguments):
            return ()
        source = arguments[index + 1]
        trailing = arguments[index + 2 :]
    elif argument.startswith("--split-string="):
        source = argument.partition("=")[2]
        trailing = arguments[index + 1 :]
    elif argument.startswith("-S") and argument != "-S":
        source = argument[2:]
        trailing = arguments[index + 1 :]
    else:
        return None
    try:
        return (*shlex.split(source, posix=True), *trailing)
    except ValueError:
        return ()


def _environment_option_width(argument: str) -> int | None:
    """Return how many argv elements a supported non-command ``env`` option consumes."""
    if argument in _ENVIRONMENT_FLAG_OPTIONS:
        return 1
    if argument in _ENVIRONMENT_VALUE_OPTIONS:
        return 2
    if argument.startswith(("--unset=", "--chdir=", "--argv0=")):
        return 1
    return None


def _command_builtin_command(arguments: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the executed operand of a supported POSIX ``command`` invocation."""
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument == "--":
            return arguments[index + 1 :] or None
        if argument == "-p":
            index += 1
            continue
        if argument in {"-v", "-V"} or argument.startswith("-"):
            return None
        return arguments[index:]
    return None


def _shell_command_runs(arguments: tuple[str, ...]) -> bool:
    """Parse supported ``sh -c`` simple commands and inspect their executable argv."""
    script_index = _shell_script_index(arguments)
    if script_index is None or script_index >= len(arguments):
        return False
    script = arguments[script_index]
    positional = arguments[script_index + 1 :]
    try:
        lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = tuple(lexer)
    except ValueError:
        return False
    segment: list[str] = []
    for token in (*tokens, ";"):
        if token in _SHELL_CONTROL_TOKENS:
            if _shell_segment_runs(tuple(segment), positional):
                return True
            segment.clear()
        else:
            segment.append(token)
    return False


def _shell_script_index(arguments: tuple[str, ...]) -> int | None:
    index = 0
    while index < len(arguments):
        option = arguments[index]
        if option == "-c" or (
            option.startswith("-") and not option.startswith("--") and "c" in option[1:]
        ):
            return index + 1
        if option in {"-o", "+o"}:
            index += 2
            continue
        if option.startswith(("-", "+")):
            index += 1
            continue
        return None
    return None


def _shell_segment_runs(segment: tuple[str, ...], positional: tuple[str, ...]) -> bool:
    resolved = tuple(_shell_positional(token, positional) for token in segment)
    index = 0
    while index < len(resolved) and (
        resolved[index] == "!" or _ENVIRONMENT_ASSIGNMENT_PATTERN.match(resolved[index])
    ):
        index += 1
    if resolved[index : index + 1] == ("exec",):
        index += 1
        if resolved[index : index + 1] == ("--",):
            index += 1
    return _may_invoke_go_run(resolved[index:]) if index < len(resolved) else False


def _shell_positional(token: str, positional: tuple[str, ...]) -> str:
    match = re.fullmatch(r"\$(?:\{(?P<braced>[0-9]+)\}|(?P<plain>[0-9]+))", token)
    if match is None:
        return token
    index = int(match.group("braced") or match.group("plain"))
    return positional[index] if index < len(positional) else ""


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

    @property
    def go_module_root(self) -> Path | None:
        """Return the validated package-owned Go module, when declared."""
        if self.metadata.go_module is None:
            return None
        return self.root / self.metadata.go_module

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


class _EvaluatorPackageRegistry:
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
    if metadata.go_module is not None:
        module_root = root / metadata.go_module
        for name in ("go.mod", "go.sum"):
            declaration = module_root / name
            if not declaration.is_file():
                raise EvaluatorPackageError.invalid_go_module(
                    module_root, f"required {name} is missing"
                )
        _validate_local_go_module_graph(root, module_root)
    return ResolvedEvaluatorPackage(
        root=root,
        metadata=metadata,
        digest=_content_digest(root),
    )


def _validate_local_go_module_graph(package_root: Path, module_root: Path) -> None:
    """Require every local replacement module to be immutable package content."""
    pending = [module_root]
    visited: set[Path] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        go_mod = current / "go.mod"
        try:
            source = go_mod.read_text(encoding="utf-8")
        except OSError as exc:
            raise EvaluatorPackageError.invalid_go_module(
                current, f"cannot read local module declaration {go_mod}: {exc}"
            ) from exc
        for line_number, directive, target_text in _local_go_replacements(source, go_mod):
            target = PurePosixPath(target_text)
            if target.is_absolute():
                detail = (
                    f"{go_mod.name}:{line_number} replace directive {directive!r} "
                    f"targets absolute local path {target_text!r}"
                )
                raise EvaluatorPackageError.invalid_go_module(current, detail)
            resolved = current.joinpath(*target.parts).resolve()
            try:
                relative = resolved.relative_to(package_root)
            except ValueError as exc:
                detail = (
                    f"{go_mod.name}:{line_number} replace directive {directive!r} "
                    f"targets {target_text!r} outside the evaluator package"
                )
                raise EvaluatorPackageError.invalid_go_module(current, detail) from exc
            excluded = _DIGEST_EXCLUDED_NAMES.intersection(relative.parts)
            if excluded:
                name = min(excluded)
                detail = (
                    f"{go_mod.name}:{line_number} replace directive {directive!r} targets "
                    f"{target_text!r} below digest-excluded path component {name!r}"
                )
                raise EvaluatorPackageError.invalid_go_module(current, detail)
            nested_go_mod = resolved / "go.mod"
            if not nested_go_mod.is_file():
                detail = (
                    f"{go_mod.name}:{line_number} replace directive {directive!r} targets "
                    f"local module {target_text!r} without go.mod"
                )
                raise EvaluatorPackageError.invalid_go_module(current, detail)
            pending.append(resolved)


def _local_go_replacements(source: str, path: Path) -> tuple[tuple[int, str, str], ...]:
    """Parse the supported Go ``replace`` grammar and return local targets."""
    replacements: list[tuple[int, str, str]] = []
    in_block = False
    for line_number, raw_line in enumerate(source.splitlines(), start=1):
        line = _strip_go_mod_comment(raw_line).strip()
        if not line:
            continue
        if not in_block and re.match(r"replace(?:\s|\()", line) is None:
            continue
        directive_tokens, in_block = _replace_tokens_for_line(
            line, in_block=in_block, path=path, line_number=line_number
        )
        if directive_tokens is None:
            continue
        target = _local_replace_target(directive_tokens, path, line_number, line)
        if target is not None:
            replacements.append((line_number, line, target))
    if in_block:
        detail = f"{path.name}: unclosed replace block"
        raise EvaluatorPackageError.invalid_go_module(path.parent, detail)
    return tuple(replacements)


def _replace_tokens_for_line(
    line: str,
    *,
    in_block: bool,
    path: Path,
    line_number: int,
) -> tuple[tuple[str, ...] | None, bool]:
    """Parse one already-selected replace line and advance its block state."""
    tokens = _go_mod_tokens(line, path, line_number)
    if in_block:
        if tokens == (")",):
            return None, False
        if "(" in tokens or ")" in tokens or tokens[:1] == ("replace",):
            _raise_unsupported_replace(path, line_number, line)
        return tokens, True
    if tokens[:1] != ("replace",):
        return None, False
    if tokens == ("replace", "("):
        return None, True
    if "(" in tokens or ")" in tokens:
        _raise_unsupported_replace(path, line_number, line)
    return tokens[1:], False


def _strip_go_mod_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if quote == "`":
            if character == quote:
                quote = None
            continue
        if character in {'"', "`"}:
            quote = character
        elif character == "/" and line[index : index + 2] == "//":
            return line[:index]
    return line


def _go_mod_tokens(line: str, path: Path, line_number: int) -> tuple[str, ...]:
    if "`" in line or "\\" in line or ";" in line:
        _raise_unsupported_replace(path, line_number, line)
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars="()=>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return tuple(lexer)
    except ValueError as exc:
        detail = f"{path.name}:{line_number} cannot parse replace directive {line!r}: {exc}"
        raise EvaluatorPackageError.invalid_go_module(path.parent, detail) from exc


def _local_replace_target(
    tokens: tuple[str, ...], path: Path, line_number: int, directive: str
) -> str | None:
    if tokens.count("=>") != 1:
        _raise_unsupported_replace(path, line_number, directive)
    separator = tokens.index("=>")
    source = tokens[:separator]
    target = tokens[separator + 1 :]
    if len(source) not in {1, 2} or len(target) not in {1, 2}:
        _raise_unsupported_replace(path, line_number, directive)
    target_name = target[0]
    local = target_name.startswith((".", "/"))
    if len(target) == 1:
        if not local:
            _raise_unsupported_replace(path, line_number, directive)
        return target_name
    if local:
        _raise_unsupported_replace(path, line_number, directive)
    return None


def _raise_unsupported_replace(path: Path, line_number: int, directive: str) -> NoReturn:
    detail = f"{path.name}:{line_number} has unsupported replace directive {directive!r}"
    raise EvaluatorPackageError.invalid_go_module(path.parent, detail)


def resolve_evaluator_package(
    packages_root: Path,
    requirement: EvaluatorPackageRequirement,
) -> ResolvedEvaluatorPackage:
    """Resolve one exact package from an explicit local collection."""
    return _EvaluatorPackageRegistry(packages_root).resolve(requirement)


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


__all__ = [
    "PACKAGE_ROOT_TOKEN",
    "TOOL_TOKEN_PREFIX",
    "EvaluatorPackageError",
    "EvaluatorPackageMetadata",
    "EvaluatorPackageNotFoundError",
    "EvaluatorPackageRequirement",
    "ResolvedEvaluatorPackage",
    "load_evaluator_package",
    "resolve_evaluator_package",
]
