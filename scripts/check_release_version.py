"""Validate release version agreement and the aggregate wheel set."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import Never, cast

from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
# Root for `scripts.*`; `packaging/` holds top-level modules (no __init__.py,
# so it cannot shadow the PyPA `packaging` library).
for _path in (REPO_ROOT, REPO_ROOT / "packaging"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from release_versions import (  # noqa: E402
    ReleaseIdentity,
    ReleaseVersionSyntaxError,
    npm_release_identity,
    python_release_identity,
)
from wheel_targets import TARGETS  # noqa: E402


class ReleaseVersionError(RuntimeError):
    """Raised when release sources, tags, or artifacts disagree on version."""

    @classmethod
    def invalid_version(cls, source: str, raw: str) -> ReleaseVersionError:
        """Build an error for a non-PEP-440 version."""
        return cls(f"{source} is not a valid PEP 440 version: {raw!r}")

    @classmethod
    def unreadable_wheel(cls, wheel: Path, cause: object) -> ReleaseVersionError:
        """Build an error for an unreadable aggregate artifact."""
        return cls(f"Cannot read wheel {wheel}: {cause}")


def check_release_version(
    tag: str | None,
    *,
    source_root: Path = REPO_ROOT,
    wheel_dir: Path | None = None,
) -> Version:
    """Return the canonical project version after checking every supplied source."""
    source_root = source_root.resolve()
    project = tomllib.loads((source_root / "pyproject.toml").read_text()).get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        _fail("pyproject.toml must declare project.version as a string")
    python_raw = cast("str", project["version"])
    project_identity = _python_identity(python_raw, source="project.version")
    version = Version(python_raw)

    tui_document = json.loads((source_root / "clients/tui/package.json").read_text())
    if not isinstance(tui_document, dict) or not isinstance(tui_document.get("version"), str):
        _fail("clients/tui/package.json must declare version as a string")
    tui_raw = cast("str", tui_document["version"])
    tui_identity = _npm_identity(tui_raw, source="TUI version")
    if tui_identity != project_identity:
        _fail(f"TUI version {tui_raw!r} does not match project version {python_raw!r}")

    if tag is not None:
        prefix = "refs/tags/v"
        if not tag.startswith(prefix) or len(tag) == len(prefix):
            _fail(f"Release tag must start with refs/tags/v, found {tag!r}")
        tag_raw = tag.removeprefix(prefix)
        tag_identity = _python_identity(tag_raw, source="release tag")
        if tag_identity != project_identity:
            _fail(f"Release tag version {tag_raw!r} does not match project version {python_raw!r}")

    if wheel_dir is not None:
        _check_wheels(wheel_dir.resolve(), version=version)
    return version


def _python_identity(raw: str, *, source: str) -> ReleaseIdentity:
    try:
        return python_release_identity(raw, source=source)
    except ReleaseVersionSyntaxError as exc:
        raise ReleaseVersionError(str(exc)) from exc


def _npm_identity(raw: str, *, source: str) -> ReleaseIdentity:
    try:
        return npm_release_identity(raw, source=source)
    except ReleaseVersionSyntaxError as exc:
        raise ReleaseVersionError(str(exc)) from exc


def _check_wheels(wheel_dir: Path, *, version: Version) -> None:
    expected = {
        f"vibesys-{version}-py3-none-{target.wheel_platform}.whl" for target in TARGETS.values()
    }
    actual = {path.name for path in wheel_dir.iterdir() if path.is_file()}
    if actual != expected:
        _fail(
            "Release directory must contain exactly the four expected wheels and no sdist: "
            f"expected {sorted(expected)}, found {sorted(actual)}"
        )
    for filename in sorted(expected):
        _check_wheel_metadata(wheel_dir / filename, version=version)


def _check_wheel_metadata(wheel: Path, *, version: Version) -> None:
    try:
        with zipfile.ZipFile(wheel) as archive:
            metadata_names = [
                name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_names) != 1:
                _fail(f"Wheel must contain exactly one METADATA file: {wheel.name}")
            metadata = BytesParser(policy=default).parsebytes(archive.read(metadata_names[0]))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseVersionError.unreadable_wheel(wheel, exc) from exc
    versions = metadata.get_all("Version", [])
    if versions != [str(version)]:
        _fail(f"Wheel {wheel.name} METADATA version does not match {version}: found {versions}")


def _fail(message: str) -> Never:
    raise ReleaseVersionError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    return parser.parse_args()


def main() -> int:
    """Validate CLI inputs and print the agreed version."""
    args = _parse_args()
    try:
        version = check_release_version(
            args.tag,
            source_root=args.source_root,
            wheel_dir=args.wheel_dir,
        )
    except (OSError, ValueError, ReleaseVersionError) as exc:
        print(f"release version check failed: {exc}", file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
