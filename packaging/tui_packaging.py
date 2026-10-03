"""Validate and stage a prebuilt, target-specific TUI payload."""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import PureWindowsPath
from typing import TYPE_CHECKING, Never, cast

from release_versions import (
    ReleaseIdentity,
    ReleaseVersionSyntaxError,
    npm_release_identity,
    python_release_identity,
)
from wheel_targets import TARGETS

if TYPE_CHECKING:
    from pathlib import Path

BUN_VERSION = "1.3.9"
_REQUIRED_FILES = (
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
)


class TuiPackagingError(RuntimeError):
    """Raised when a release TUI payload is absent, incomplete, or inconsistent."""


def stage_prebuilt_tui(
    source: Path | None,
    destination: Path,
    *,
    required: bool,
    expected_target: str | None = None,
    expected_distribution_version: str | None = None,
) -> bool:
    """Validate and copy a prepared TUI payload into the wheel build tree."""
    if source is None or not source.is_dir():
        if required:
            _fail("Required TUI payload directory is missing")
        return False

    validate_tui_payload(
        source,
        expected_target=expected_target,
        expected_distribution_version=expected_distribution_version,
    )
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    return True


def validate_tui_payload(
    root: Path,
    *,
    expected_target: str | None = None,
    expected_distribution_version: str | None = None,
) -> None:
    """Validate a complete TUI payload without copying it."""
    manifest = _load_manifest(root / "manifest.json")
    target_key, manifest_version = _validate_manifest(manifest, expected_target=expected_target)
    _validate_required_files(root)
    _validate_payload_version(
        root,
        manifest_version=manifest_version,
        expected_distribution_version=expected_distribution_version,
    )
    _validate_tree_shape(root)
    _validate_native_package(root, target_key=target_key)
    _validate_hashes(root, manifest)


def _validate_manifest(
    manifest: dict[str, object],
    *,
    expected_target: str | None,
) -> tuple[str, str]:
    target_key = _manifest_string(manifest, "target")
    if target_key not in TARGETS:
        _fail(f"Unsupported TUI payload target: {target_key}")
    if expected_target is not None and target_key != expected_target:
        _fail(f"TUI payload target {target_key!r} does not match {expected_target!r}")
    if _manifest_string(manifest, "bun_version") != BUN_VERSION:
        _fail(f"TUI payload must use Bun version {BUN_VERSION}")
    tui_version = _manifest_string(manifest, "tui_version")
    _npm_identity(tui_version, source="TUI payload manifest version")
    if manifest.get("schema_version") != 1:
        _fail("Unsupported TUI payload manifest schema")
    return target_key, tui_version


def _validate_payload_version(
    root: Path,
    *,
    manifest_version: str,
    expected_distribution_version: str | None,
) -> None:
    try:
        document = json.loads((root / "app" / "package.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        error = TuiPackagingError("TUI payload app/package.json is invalid")
        raise error from exc
    if not isinstance(document, dict) or not isinstance(document.get("version"), str):
        _fail("TUI payload app/package.json must declare version as a string")
    _validate_portable_dependencies(cast("dict[str, object]", document))
    package_version = cast("str", document["version"])
    package_identity = _npm_identity(package_version, source="TUI payload package.json version")
    manifest_identity = _npm_identity(manifest_version, source="TUI payload manifest version")
    if package_identity != manifest_identity or package_version != manifest_version:
        _fail(
            f"TUI payload package.json version {package_version!r} does not match "
            f"manifest version {manifest_version!r}"
        )
    if expected_distribution_version is None:
        return
    try:
        distribution_identity = python_release_identity(
            expected_distribution_version,
            source="Python distribution version",
        )
    except ReleaseVersionSyntaxError as exc:
        raise TuiPackagingError(str(exc)) from exc
    if manifest_identity != distribution_identity:
        _fail(
            f"TUI payload version {manifest_version!r} does not match Python distribution "
            f"version {expected_distribution_version!r}"
        )


def _validate_portable_dependencies(document: dict[str, object]) -> None:
    raw_dependencies = document.get("dependencies", {})
    if not isinstance(raw_dependencies, dict):
        _fail("TUI payload app/package.json dependencies must be an object")
    for name, value in raw_dependencies.items():
        if not isinstance(name, str) or not isinstance(value, str):
            _fail("TUI payload app/package.json dependencies must contain strings")
        is_windows_absolute = PureWindowsPath(value).is_absolute()
        if "file:" in value or value.startswith(("/", "\\", "./", "../")) or is_windows_absolute:
            _fail(f"TUI payload dependency {name!r} contains a local path")


def _npm_identity(raw: str, *, source: str) -> ReleaseIdentity:
    try:
        return npm_release_identity(raw, source=source)
    except ReleaseVersionSyntaxError as exc:
        raise TuiPackagingError(str(exc)) from exc


def _validate_required_files(root: Path) -> None:
    for relative in _REQUIRED_FILES:
        if not (root / relative).is_file():
            _fail(f"TUI payload is missing {relative}")

    runtime_mode = (root / "bin" / "bun").stat().st_mode
    if not runtime_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
        _fail("Bundled Bun runtime is not executable")


def _validate_tree_shape(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            _fail(f"TUI payload contains a symlink: {path.relative_to(root)}")
        if path.name.endswith(".map"):
            _fail(f"TUI payload contains a source map: {path.relative_to(root)}")


def _validate_native_package(root: Path, *, target_key: str) -> None:
    target = TARGETS[target_key]
    native_root = root / "app" / "node_modules" / "@opentui"
    native_packages = {
        f"@opentui/{path.name}"
        for path in native_root.iterdir()
        if path.is_dir() and path.name.startswith("core-")
    }
    if native_packages != {target.opentui_package}:
        _fail(
            "TUI payload must contain exactly its target OpenTUI native package; "
            f"found {sorted(native_packages)}"
        )


def _validate_hashes(root: Path, manifest: dict[str, object]) -> None:
    raw_hashes = manifest.get("files")
    if not isinstance(raw_hashes, dict) or not all(
        isinstance(path, str) and isinstance(digest, str) for path, digest in raw_hashes.items()
    ):
        _fail("TUI payload manifest has invalid file hashes")
    expected_hashes = cast("dict[str, str]", raw_hashes)
    actual_files = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }
    if set(expected_hashes) != actual_files:
        _fail("TUI payload manifest file list does not match its contents")
    for relative, expected_hash in expected_hashes.items():
        actual_hash = hashlib.sha256((root / relative).read_bytes()).hexdigest()
        if actual_hash != expected_hash:
            _fail(f"TUI payload hash mismatch for {relative}")


def _load_manifest(path: Path) -> dict[str, object]:
    if not path.is_file():
        _fail("TUI payload is missing manifest.json")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        error = TuiPackagingError("TUI payload manifest.json is invalid")
        raise error from exc
    if not isinstance(value, dict):
        _fail("TUI payload manifest.json must contain an object")
    return cast("dict[str, object]", value)


def _manifest_string(manifest: dict[str, object], key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str):
        _fail(f"TUI payload manifest field {key!r} must be a string")
    return value


def _fail(message: str) -> Never:
    raise TuiPackagingError(message)
