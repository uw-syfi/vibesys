"""Validate and identify browser bundles at the launcher boundary."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

WEB_BUILD_MANIFEST = ".vibesys-web-build.json"
WEB_BUILD_COMMAND = "pnpm --dir clients/web build"

_SHA256_PREFIX = "sha256:"
_SHA256_HEX_LENGTH = 64


class _WebAssetError(ValueError):
    """An actionable browser-artifact validation failure."""

    @classmethod
    def source_path(cls) -> Self:
        return cls("source path must stay within the workspace")

    @classmethod
    def source_file_path(cls) -> Self:
        return cls("source file path must stay within its source tree")

    @classmethod
    def source_digest(cls) -> Self:
        return cls("source file digest must be lowercase SHA-256")

    @classmethod
    def workspace_path(cls) -> Self:
        return cls("workspace path must be relative to the bundle")

    @classmethod
    def missing_manifest(cls, bundle: Path) -> Self:
        return cls(
            f"web assets {bundle} have no {WEB_BUILD_MANIFEST}; run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def workspace_mismatch(cls, manifest: Path) -> Self:
        return cls(
            f"web build manifest {manifest} names a workspace that does not contain the bundle; "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def repeated_source(cls, manifest: Path, source: str) -> Self:
        return cls(
            f"web build manifest {manifest} repeats source {source!r}; "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def source_outside_workspace(cls, manifest: Path) -> Self:
        return cls(
            f"web build manifest {manifest} names source outside its workspace; "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def stale(cls, bundle: Path, source: Path, build_id: str) -> Self:
        return cls(
            f"web assets {bundle} are stale: source {source} differs from build {build_id}; "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def changed_asset(cls, bundle: Path, asset: Path, build_id: str) -> Self:
        return cls(
            f"web assets {bundle} do not match build {build_id}: asset {asset} differs; "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )

    @classmethod
    def invalid_manifest(cls, manifest: Path, error: BaseException) -> Self:
        return cls(
            f"web build manifest {manifest} is invalid ({error}); "
            f"run `{WEB_BUILD_COMMAND}` and retry"
        )


class _ManifestSource(BaseModel):
    """One source tree captured by the web build."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    files: dict[str, str]

    @field_validator("path")
    @classmethod
    def _relative_source_path(cls, value: str) -> str:
        if Path(value).is_absolute() or ".." in Path(value).parts:
            raise _WebAssetError.source_path()
        return value

    @field_validator("files")
    @classmethod
    def _source_files(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_snapshot(value)


class _WebBuildManifest(BaseModel):
    """The strict build-to-launch contract emitted by the web client."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    build_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace: str = Field(min_length=1)
    sources: tuple[_ManifestSource, ...] = Field(min_length=1)
    assets: dict[str, str]

    @field_validator("workspace")
    @classmethod
    def _relative_workspace_path(cls, value: str) -> str:
        if Path(value).is_absolute():
            raise _WebAssetError.workspace_path()
        return value

    @field_validator("assets")
    @classmethod
    def _asset_files(cls, value: dict[str, str]) -> dict[str, str]:
        return _validate_snapshot(value)


@dataclass(frozen=True)
class WebAssetBundle:
    """A resolved browser bundle and its externally visible identity."""

    directory: Path
    build_id: str

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        require_manifest: bool,
    ) -> WebAssetBundle:
        """Inspect ``directory`` and reject a manifest that no longer matches its sources."""
        resolved = directory.expanduser().resolve()
        manifest_path = resolved / WEB_BUILD_MANIFEST
        if not manifest_path.is_file():
            if require_manifest:
                raise _WebAssetError.missing_manifest(resolved)
            return cls(resolved, _unmanaged_build_id(resolved))

        manifest = _read_manifest(manifest_path)
        workspace = (resolved / manifest.workspace).resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError:
            raise _WebAssetError.workspace_mismatch(manifest_path) from None

        seen_sources: set[str] = set()
        for source in manifest.sources:
            if source.path in seen_sources:
                raise _WebAssetError.repeated_source(manifest_path, source.path)
            seen_sources.add(source.path)
            source_root = (workspace / source.path).resolve()
            try:
                source_root.relative_to(workspace)
            except ValueError:
                raise _WebAssetError.source_outside_workspace(manifest_path) from None
            changed = _first_changed_file(source_root, source.files)
            if changed is not None:
                raise _WebAssetError.stale(resolved, changed, manifest.build_id)
        changed_asset = _first_changed_file(
            resolved,
            manifest.assets,
            ignored=frozenset({WEB_BUILD_MANIFEST}),
        )
        if changed_asset is not None:
            raise _WebAssetError.changed_asset(resolved, changed_asset, manifest.build_id)
        return cls(resolved, manifest.build_id)


def _read_manifest(path: Path) -> _WebBuildManifest:
    try:
        return _WebBuildManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError) as error:
        raise _WebAssetError.invalid_manifest(path, error) from None


def _validate_snapshot(value: dict[str, str]) -> dict[str, str]:
    for path, digest in value.items():
        if not path or Path(path).is_absolute() or ".." in Path(path).parts:
            raise _WebAssetError.source_file_path()
        if len(digest) != _SHA256_HEX_LENGTH or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise _WebAssetError.source_digest()
    return value


def _first_changed_file(
    source_root: Path,
    expected: dict[str, str],
    *,
    ignored: frozenset[str] = frozenset(),
) -> Path | None:
    try:
        current = _file_snapshot(source_root)
    except OSError:
        return source_root
    for relative in ignored:
        current.pop(relative, None)
    for relative in sorted(expected.keys() | current.keys()):
        if expected.get(relative) != current.get(relative):
            return source_root / relative
    return None


def _file_snapshot(root: Path) -> dict[str, str]:
    if not root.is_dir():
        raise FileNotFoundError(root)
    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        snapshot[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return snapshot


def _unmanaged_build_id(directory: Path) -> str:
    digest = hashlib.sha256()
    try:
        snapshot = _file_snapshot(directory)
    except OSError:
        snapshot = {}
    for relative, file_digest in snapshot.items():
        if relative == WEB_BUILD_MANIFEST:
            continue
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\0")
    return f"{_SHA256_PREFIX}{digest.hexdigest()}"
