"""Statically verify one self-contained VibeSys release wheel."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import subprocess
import sys
import tomllib
import zipfile
from collections import Counter
from email.parser import BytesParser
from email.policy import default
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Never, cast

from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from release_versions import (  # noqa: E402
    ReleaseVersionSyntaxError,
    npm_release_identity,
    python_release_identity,
)
from tui_packaging import BUN_VERSION  # noqa: E402
from wheel_targets import TARGETS, WheelTarget  # noqa: E402

if TYPE_CHECKING:
    from email.message import Message

PYPI_FILE_SIZE_LIMIT = 100_000_000
FRAMEWORK_PACKAGES = (
    "vibesys",
    "entrypoints",
    "server",
    "vs_correctness",
    "vs_evaluator_protocol",
    "vs_feature_flags",
    "vs_github",
    "vs_issue_board",
    "vs_loop_state",
    "vs_project",
    "vs_prompts",
    "vs_sandbox",
)
_INTERNAL_DISTRIBUTIONS = frozenset(
    {
        "vs-evaluator-protocol",
        "vs-correctness",
        "vs-feature-flags",
        "vs-github",
        "vs-issue-board",
        "vs-loop-state",
        "vs-project",
        "vs-prompts",
        "vs-sandbox",
    }
)
_PACKAGE_SOURCE_ROOTS = {
    Path("src/vibesys"): PurePosixPath("vibesys"),
    Path("src/entrypoints"): PurePosixPath("entrypoints"),
    Path("src/server"): PurePosixPath("server"),
    Path("libs/vs-correctness/src/vs_correctness"): PurePosixPath("vs_correctness"),
    Path("libs/vs-evaluator-protocol/src/vs_evaluator_protocol"): PurePosixPath(
        "vs_evaluator_protocol"
    ),
    Path("libs/vs-feature-flags/src/vs_feature_flags"): PurePosixPath("vs_feature_flags"),
    Path("libs/vs-github/src/vs_github"): PurePosixPath("vs_github"),
    Path("libs/vs-issue-board/src/vs_issue_board"): PurePosixPath("vs_issue_board"),
    Path("libs/vs-loop-state/src/vs_loop_state"): PurePosixPath("vs_loop_state"),
    Path("libs/vs-project/src/vs_project"): PurePosixPath("vs_project"),
    Path("libs/vs-prompts/src/vs_prompts"): PurePosixPath("vs_prompts"),
    Path("libs/vs-sandbox/src/vs_sandbox"): PurePosixPath("vs_sandbox"),
    Path("resources/evaluators"): PurePosixPath("vibesys/_resources/evaluators"),
    Path("resources/profilers"): PurePosixPath("vibesys/_resources/profilers"),
    Path("resources/skills"): PurePosixPath("vibesys/_resources/skills"),
}
_EXCLUDED_PARTS = frozenset(
    {
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
        "repos",
        "target",
    }
)
_REPOSITORY_ARCHIVE_ROOTS = frozenset(
    {".git", "clients", "libs", "resources", "sdk", "src", "tests", "third_party"}
)
_REQUIRED_TUI_FILES = (
    "bin/bun",
    "app/dist/launcher.js",
    "app/dist/self-test.js",
    "app/package.json",
    "app/node_modules/@vibesys/backend-client/package.json",
    "app/node_modules/@vibesys/backend-client/dist/index.js",
    "app/node_modules/@vibesys/core-state/package.json",
    "app/node_modules/@vibesys/core-state/dist/index.js",
    "app/node_modules/@opentui/core/index.js",
    "licenses/BUN-LICENSE.md",
    "licenses/opentui-core.txt",
    "manifest.json",
)
_EXPECTED_ENTRY_POINTS = {
    "vibesys": "entrypoints.launcher:main",
    "vibesys-issue-mcp": "vs_issue_board.mcp:main",
}
_DIST_INFO_FILES = frozenset(
    {
        "METADATA",
        "WHEEL",
        "entry_points.txt",
        "licenses/LICENSE",
        "RECORD",
        "top_level.txt",
    }
)
_SUPPORTED_METADATA_VERSIONS = frozenset({"2.1", "2.2", "2.3", "2.4"})
_SUPPORTED_WHEEL_VERSIONS = frozenset({"1.0"})


class ReleaseWheelError(RuntimeError):
    """Raised when a wheel violates the release artifact contract."""

    @classmethod
    def unreadable_wheel(cls, wheel: Path, cause: object) -> ReleaseWheelError:
        """Build an error for an unreadable ZIP wheel."""
        return cls(f"Cannot read wheel {wheel}: {cause}")

    @classmethod
    def unreadable_project(cls, path: Path, cause: object) -> ReleaseWheelError:
        """Build an error for unreadable project metadata."""
        return cls(f"Cannot read project metadata from {path}: {cause}")

    @classmethod
    def missing_member(cls, name: str) -> ReleaseWheelError:
        """Build an error for missing wheel metadata."""
        return cls(f"Wheel is missing {name}")

    @classmethod
    def invalid_requirement(cls, cause: object) -> ReleaseWheelError:
        """Build an error for invalid dependency metadata."""
        return cls(f"Wheel contains invalid dependency metadata: {cause}")

    @classmethod
    def untracked_source_root(cls, source_root: Path) -> ReleaseWheelError:
        """Build an error for a source root Git cannot enumerate."""
        return cls(f"Cannot enumerate tracked source files in {source_root}")

    @classmethod
    def invalid_manifest(cls) -> ReleaseWheelError:
        """Build an error for malformed payload JSON."""
        return cls("TUI payload manifest.json is invalid")


def verify_wheel(wheel: Path, source_root: Path, target: WheelTarget) -> None:
    """Verify ``wheel`` against tracked sources and the requested native target."""
    wheel = wheel.resolve()
    source_root = source_root.resolve()
    project = _load_project(source_root)
    version = _project_string(project, "version")
    expected_suffix = f"-py3-none-{target.wheel_platform}.whl"
    if not wheel.name.endswith(expected_suffix):
        _fail(
            f"Wheel filename has the wrong platform tag: {wheel.name}; expected {expected_suffix}"
        )
    expected_name = f"vibesys-{version}-py3-none-{target.wheel_platform}.whl"
    if wheel.name != expected_name:
        _fail(
            f"Wheel filename version does not match pyproject.toml: "
            f"expected {expected_name}, found {wheel.name}"
        )
    if wheel.stat().st_size > PYPI_FILE_SIZE_LIMIT:
        _fail(f"Wheel exceeds the PyPI 100 MB file limit: {wheel.stat().st_size} bytes")

    dist_info = f"vibesys-{version}.dist-info"
    platlib = ""
    try:
        with zipfile.ZipFile(wheel) as archive:
            infos = archive.infolist()
            _verify_archive_paths(infos)
            members = {info.filename: info for info in infos}
            metadata = _read_metadata(archive, f"{dist_info}/METADATA")
            wheel_metadata = _read_metadata(archive, f"{dist_info}/WHEEL")
            _verify_metadata(metadata, project=project, version=version)
            _verify_wheel_metadata(wheel_metadata, target=target)
            _verify_framework_packages(archive, members, platlib=platlib)
            source_members = _verify_tracked_sources(
                archive,
                members,
                source_root=source_root,
                platlib=platlib,
            )
            _verify_entry_points(archive, members, dist_info=dist_info)
            tui_members = _verify_tui_payload(
                archive,
                members,
                platlib=platlib,
                target=target,
                version=version,
            )
            _verify_repository_licenses(
                archive,
                members,
                source_root=source_root,
                platlib=platlib,
                dist_info=dist_info,
            )
            dist_info_members = {f"{dist_info}/{name}" for name in _DIST_INFO_FILES}
            for name in dist_info_members:
                _required_member(members, name, "wheel metadata")
            _verify_exact_members(
                infos,
                allowed=source_members | tui_members | dist_info_members,
            )
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseWheelError.unreadable_wheel(wheel, exc) from exc


def _load_project(source_root: Path) -> dict[str, object]:
    path = source_root / "pyproject.toml"
    try:
        document = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseWheelError.unreadable_project(path, exc) from exc
    project = document.get("project")
    if not isinstance(project, dict):
        _fail(f"Project metadata is missing from {path}")
    return cast("dict[str, object]", project)


def _project_string(project: dict[str, object], key: str) -> str:
    value = project.get(key)
    if not isinstance(value, str):
        _fail(f"Project metadata field {key!r} must be a string")
    return value


def _verify_archive_paths(infos: list[zipfile.ZipInfo]) -> None:
    names = [info.filename for info in infos]
    duplicates = sorted(name for name, count in Counter(names).items() if count > 1)
    if duplicates:
        _fail(f"Wheel contains duplicate archive names: {duplicates}")
    for info in infos:
        name = info.filename
        path = PurePosixPath(name)
        if (
            not name
            or "\\" in name
            or path.is_absolute()
            or PureWindowsPath(name).is_absolute()
            or ".." in path.parts
            or (path.parts and path.parts[0] in _REPOSITORY_ARCHIVE_ROOTS)
        ):
            _fail(f"Wheel contains an unsafe or repository archive path: {name!r}")
        canonical_name = path.as_posix() + ("/" if info.is_dir() else "")
        archive_parts = name[:-1].split("/") if info.is_dir() else name.split("/")
        if (
            name != canonical_name
            or not path.parts
            or any(part in {"", "."} for part in archive_parts)
        ):
            _fail(f"Wheel contains a non-canonical archive path: {name!r}")
        if name.endswith(".map"):
            _fail(f"Wheel contains a source map archive path: {name}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            _fail(f"Wheel contains a symlink archive path: {name}")


def _read_metadata(archive: zipfile.ZipFile, name: str) -> Message:
    try:
        content = archive.read(name)
    except KeyError as exc:
        raise ReleaseWheelError.missing_member(name) from exc
    return BytesParser(policy=default).parsebytes(content)


def _verify_metadata(
    metadata: Message,
    *,
    project: dict[str, object],
    version: str,
) -> None:
    metadata_version = _singleton_header(metadata, "Metadata-Version")
    if metadata_version not in _SUPPORTED_METADATA_VERSIONS:
        _fail(f"Wheel METADATA has unsupported Metadata-Version: {metadata_version!r}")
    name = _singleton_header(metadata, "Name")
    if name != "vibesys":
        _fail(f"Wheel METADATA has the wrong name: {name!r}")
    wheel_version = _singleton_header(metadata, "Version")
    if wheel_version != version:
        _fail(
            f"Wheel METADATA version {wheel_version!r} does not match project version {version!r}"
        )
    expected_python = _project_string(project, "requires-python")
    if _singleton_header(metadata, "Requires-Python") != expected_python:
        _fail("Wheel METADATA Requires-Python does not match pyproject.toml")

    raw_requirements = metadata.get_all("Requires-Dist", [])
    try:
        actual = Counter(str(Requirement(value)) for value in raw_requirements)
    except InvalidRequirement as exc:
        raise ReleaseWheelError.invalid_requirement(exc) from exc
    for requirement in actual:
        parsed = Requirement(requirement)
        canonical_name = canonicalize_name(parsed.name)
        if canonical_name in _INTERNAL_DISTRIBUTIONS:
            _fail(f"Wheel must not depend on internal distribution {parsed.name}")
        if canonical_name == "mcp" and parsed.specifier.contains("2.0.0", prereleases=True):
            _fail("Wheel MCP dependency must exclude MCP 2.0.0")

    expected_values = _project_requirements(project)
    expected = Counter(str(Requirement(value)) for value in expected_values)
    if actual != expected:
        _fail(
            "Wheel dependency metadata does not match pyproject.toml: "
            f"expected {sorted(expected.elements())}, found {sorted(actual.elements())}"
        )


def _project_requirements(project: dict[str, object]) -> list[str]:
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or not all(
        isinstance(value, str) for value in dependencies
    ):
        _fail("Project dependencies must be a list of strings")
    result = list(cast("list[str]", dependencies))
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        _fail("Project optional dependencies must be a table")
    for extra, values in optional.items():
        if (
            not isinstance(extra, str)
            or not isinstance(values, list)
            or not all(isinstance(value, str) for value in values)
        ):
            _fail("Project optional dependencies must contain lists of strings")
        result.extend(f'{value}; extra == "{extra}"' for value in cast("list[str]", values))
    return result


def _verify_wheel_metadata(metadata: Message, *, target: WheelTarget) -> None:
    wheel_version = _singleton_header(metadata, "Wheel-Version")
    if wheel_version not in _SUPPORTED_WHEEL_VERSIONS:
        _fail(f"Wheel metadata has unsupported Wheel-Version: {wheel_version!r}")
    expected_tag = f"py3-none-{target.wheel_platform}"
    if _singleton_header(metadata, "Root-Is-Purelib").lower() != "false":
        _fail("Native release wheel must declare Root-Is-Purelib: false")
    tag = _singleton_header(metadata, "Tag")
    if tag != expected_tag:
        _fail(
            f"Wheel metadata has the wrong platform tag; expected {expected_tag!r}, found {tag!r}"
        )


def _singleton_header(metadata: Message, name: str) -> str:
    values = metadata.get_all(name, [])
    if len(values) != 1:
        _fail(f"Wheel metadata must contain exactly one {name}; found {len(values)}")
    return str(values[0])


def _verify_framework_packages(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    platlib: str,
) -> None:
    for package in FRAMEWORK_PACKAGES:
        _required_member(
            members,
            _archive_path(platlib, f"{package}/__init__.py"),
            f"framework package {package}",
        )
    for package in FRAMEWORK_PACKAGES[1:]:
        _required_member(
            members,
            _archive_path(platlib, f"{package}/py.typed"),
            f"{package} py.typed",
        )

    top_level_path = next(
        (name for name in members if name.endswith(".dist-info/top_level.txt")),
        None,
    )
    if top_level_path is not None:
        declared = set(archive.read(top_level_path).decode().splitlines())
        if declared != set(FRAMEWORK_PACKAGES):
            _fail(
                "Wheel top_level.txt does not declare exactly the framework packages: "
                f"{sorted(declared)}"
            )


def _verify_tracked_sources(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    source_root: Path,
    platlib: str,
) -> set[str]:
    tracked = _tracked_files(source_root)
    expected = _expected_packaged_sources(source_root, tracked=tracked, platlib=platlib)

    for source_relative, archive_name in expected.items():
        _required_member(members, archive_name, source_relative.as_posix())
        source_digest = hashlib.sha256((source_root / source_relative).read_bytes()).digest()
        archive_digest = hashlib.sha256(archive.read(archive_name)).digest()
        if archive_digest != source_digest:
            _fail(f"Wheel source digest mismatch for {source_relative.as_posix()}")
    return set(expected.values())


def _expected_packaged_sources(
    source_root: Path,
    *,
    tracked: tuple[Path, ...],
    platlib: str,
) -> dict[Path, str]:
    expected: dict[Path, str] = {}
    for source_prefix, package_prefix in _PACKAGE_SOURCE_ROOTS.items():
        for relative in tracked:
            if not relative.is_relative_to(source_prefix) or _excluded(relative):
                continue
            source = source_root / relative
            if not source.is_file():
                _fail(f"Wheel tracked source is missing from worktree: {relative.as_posix()}")
            suffix = relative.relative_to(source_prefix).as_posix()
            expected[relative] = _archive_path(
                platlib,
                f"{package_prefix.as_posix()}/{suffix}",
            )

    sdk_root = Path("sdk/vs-bench")
    for relative in tracked:
        if not relative.is_relative_to(sdk_root) or _excluded(relative):
            continue
        sdk_relative = relative.relative_to(sdk_root)
        if sdk_relative.parts[0] not in {"README.md", "pyproject.toml", "src"}:
            continue
        source = source_root / relative
        if not source.is_file():
            _fail(f"Wheel tracked source is missing from worktree: {relative.as_posix()}")
        expected[relative] = _archive_path(
            platlib,
            f"vibesys/_sdk/vs-bench/{sdk_relative.as_posix()}",
        )
    return expected


def _tracked_files(source_root: Path) -> tuple[Path, ...]:
    git = shutil.which("git")
    if git is None:
        raise ReleaseWheelError.untracked_source_root(source_root)
    try:
        result = subprocess.run(  # noqa: S603
            [git, "-C", str(source_root), "ls-files", "-z"],
            check=True,
            capture_output=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ReleaseWheelError.untracked_source_root(source_root) from exc
    return tuple(Path(value.decode()) for value in result.stdout.split(b"\0") if value)


def _excluded(path: Path) -> bool:
    return bool(_EXCLUDED_PARTS.intersection(path.parts)) or path.name.endswith(
        (".pyc", ".egg-info")
    )


def _verify_entry_points(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    dist_info: str,
) -> None:
    name = f"{dist_info}/entry_points.txt"
    _required_member(members, name, "console scripts")
    section: str | None = None
    actual: dict[str, str] = {}
    for raw_line in archive.read(name).decode().splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "console_scripts" and "=" in line:
            key, value = line.split("=", maxsplit=1)
            actual[key.strip()] = value.strip()
    if actual != _EXPECTED_ENTRY_POINTS:
        _fail(f"Wheel console scripts are incomplete or unexpected: {actual}")


def _verify_tui_payload(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    platlib: str,
    target: WheelTarget,
    version: str,
) -> set[str]:
    root = _archive_path(platlib, "entrypoints/_tui")
    for relative in _REQUIRED_TUI_FILES:
        _required_member(members, f"{root}/{relative}", relative)

    runtime_info = members[f"{root}/bin/bun"]
    if not (runtime_info.external_attr >> 16) & 0o111:
        _fail("Bundled Bun runtime is not executable")

    manifest = _load_manifest(archive.read(f"{root}/manifest.json"))
    if manifest.get("schema_version") != 1:
        _fail("Unsupported TUI payload manifest schema")
    if manifest.get("target") != target.key:
        _fail(f"TUI payload target {manifest.get('target')!r} does not match {target.key!r}")
    if manifest.get("bun_version") != BUN_VERSION:
        _fail(f"TUI payload must use Bun version {BUN_VERSION}")
    raw_manifest_version = manifest.get("tui_version")
    if not isinstance(raw_manifest_version, str):
        _fail("TUI payload manifest version must be a string")
    package_document = _load_manifest(archive.read(f"{root}/app/package.json"))
    _verify_portable_tui_dependencies(package_document)
    raw_package_version = package_document.get("version")
    if not isinstance(raw_package_version, str):
        _fail("TUI payload app/package.json version must be a string")
    try:
        distribution_identity = python_release_identity(version, source="wheel version")
        manifest_identity = npm_release_identity(
            raw_manifest_version,
            source="TUI payload manifest version",
        )
        package_identity = npm_release_identity(
            raw_package_version,
            source="TUI payload package.json version",
        )
    except ReleaseVersionSyntaxError as exc:
        raise ReleaseWheelError(str(exc)) from exc
    if (
        manifest_identity != distribution_identity
        or package_identity != distribution_identity
        or raw_manifest_version != raw_package_version
    ):
        _fail("TUI payload manifest and package versions do not match the Python distribution")
    manifest_members = _verify_manifest_hashes(archive, members, root=root, manifest=manifest)
    _verify_native_package(members, root=root, target=target)
    return manifest_members


def _verify_portable_tui_dependencies(package: dict[str, object]) -> None:
    raw_dependencies = package.get("dependencies", {})
    if not isinstance(raw_dependencies, dict):
        _fail("TUI payload app/package.json dependencies must be an object")
    for name, value in raw_dependencies.items():
        if not isinstance(name, str) or not isinstance(value, str):
            _fail("TUI payload app/package.json dependencies must contain strings")
        is_windows_absolute = PureWindowsPath(value).is_absolute()
        if "file:" in value or value.startswith(("/", "\\", "./", "../")) or is_windows_absolute:
            _fail(f"TUI payload dependency {name!r} contains a local path")


def _load_manifest(content: bytes) -> dict[str, object]:
    try:
        value = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ReleaseWheelError.invalid_manifest() from exc
    if not isinstance(value, dict):
        _fail("TUI payload manifest.json must contain an object")
    return cast("dict[str, object]", value)


def _verify_manifest_hashes(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    root: str,
    manifest: dict[str, object],
) -> set[str]:
    raw_hashes = manifest.get("files")
    if not isinstance(raw_hashes, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in raw_hashes.items()
    ):
        _fail("TUI payload manifest has invalid file hashes")
    expected_hashes = cast("dict[str, str]", raw_hashes)
    prefix = f"{root}/"
    actual = {
        name.removeprefix(prefix)
        for name in members
        if name.startswith(prefix) and name != f"{root}/manifest.json" and not name.endswith("/")
    }
    if set(expected_hashes) != actual:
        _fail("TUI payload manifest file list does not match its contents")
    for relative, digest in expected_hashes.items():
        actual_digest = hashlib.sha256(archive.read(f"{root}/{relative}")).hexdigest()
        if actual_digest != digest:
            _fail(f"TUI payload hash mismatch for {relative}")
    return {f"{root}/manifest.json"} | {f"{root}/{relative}" for relative in expected_hashes}


def _verify_repository_licenses(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    source_root: Path,
    platlib: str,
    dist_info: str,
) -> None:
    _verify_source_digest(
        archive,
        members,
        source=source_root / "third_party/bun/LICENSE",
        member=_archive_path(platlib, "entrypoints/_tui/licenses/BUN-LICENSE.md"),
        description="Bun license",
    )
    _verify_source_digest(
        archive,
        members,
        source=source_root / "LICENSE",
        member=f"{dist_info}/licenses/LICENSE",
        description="root license",
    )


def _verify_source_digest(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    *,
    source: Path,
    member: str,
    description: str,
) -> None:
    if not source.is_file():
        _fail(f"Repository {description} is missing: {source}")
    _required_member(members, member, description)
    if (
        hashlib.sha256(archive.read(member)).digest()
        != hashlib.sha256(source.read_bytes()).digest()
    ):
        _fail(f"Wheel {description} digest does not match {source.name}")


def _archive_path(root: str, relative: str) -> str:
    return f"{root}/{relative}" if root else relative


def _verify_exact_members(
    infos: list[zipfile.ZipInfo],
    *,
    allowed: set[str],
) -> None:
    directories = {info.filename for info in infos if info.is_dir()}
    aliases = sorted(name for name in directories if name.removesuffix("/") in allowed)
    if aliases:
        _fail(f"Wheel directory entry aliases an allowed file: {aliases}")

    entries_by_path: dict[str, str] = {}
    for info in infos:
        path = info.filename.removesuffix("/") if info.is_dir() else info.filename
        existing = entries_by_path.get(path)
        if existing is not None:
            _fail(f"Wheel contains colliding archive entries: {existing!r}, {info.filename!r}")
        entries_by_path[path] = info.filename

    expected_directories = {
        f"{parent.as_posix()}/"
        for name in allowed
        for parent in PurePosixPath(name).parents
        if parent != PurePosixPath(".")
    }
    unexpected_directories = sorted(directories - expected_directories)
    if unexpected_directories:
        _fail(f"Wheel contains unexpected directory entries: {unexpected_directories}")

    actual = {info.filename for info in infos if not info.is_dir()}
    unexpected = sorted(actual - allowed)
    if unexpected:
        _fail(f"Wheel contains unexpected files: {unexpected}")


def _verify_native_package(
    members: dict[str, zipfile.ZipInfo],
    *,
    root: str,
    target: WheelTarget,
) -> None:
    prefix = f"{root}/app/node_modules/@opentui/"
    native_packages = {
        f"@opentui/{remainder.split('/', maxsplit=1)[0]}"
        for name in members
        if name.startswith(prefix)
        for remainder in [name.removeprefix(prefix)]
        if remainder.startswith("core-") and "/" in remainder
    }
    if native_packages != {target.opentui_package}:
        _fail(
            "TUI payload must contain exactly its target OpenTUI native package; "
            f"found {sorted(native_packages)}"
        )


def _required_member(
    members: dict[str, zipfile.ZipInfo],
    name: str,
    description: str,
) -> None:
    if name not in members:
        _fail(f"Wheel is missing {description}: {name}")


def _fail(message: str) -> Never:
    raise ReleaseWheelError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--target", required=True, choices=sorted(TARGETS))
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    return parser.parse_args()


def main() -> int:
    """Verify the requested wheel and report a concise result."""
    args = _parse_args()
    try:
        verify_wheel(args.wheel, args.source_root, TARGETS[args.target])
    except ReleaseWheelError as exc:
        print(f"release wheel verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"verified {args.wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
