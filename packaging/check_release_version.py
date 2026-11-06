"""Validate release version agreement and the aggregate wheel set."""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import zipfile
from email.parser import BytesParser
from email.policy import default
from pathlib import Path
from typing import TYPE_CHECKING, Never, cast

from evaluator_wheels import release_names
from packaging.version import Version
from release_versions import (
    ReleaseIdentity,
    ReleaseVersionSyntaxError,
    go_module_version,
    python_release_identity,
)

if TYPE_CHECKING:
    from collections.abc import Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG_PREFIX = "refs/tags/v"


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
    tag_prefix: str = DEFAULT_TAG_PREFIX,
    expected_files: Callable[[str], set[str]] | None = None,
) -> Version:
    """Return the canonical project version after checking every supplied source.

    ``source_root`` holds the ``pyproject.toml`` of the distribution being released, and
    ``tag_prefix`` is what its release tags start with before the version (``refs/tags/v`` for
    ``vibesys``, ``refs/tags/evaluator-v`` for ``vibesys-evaluator``). ``wheel_dir`` must hold
    exactly the one universal wheel, unless ``expected_files`` names the release's files from the
    version (the evaluator's platform wheels and sdist): then it holds exactly those.
    """
    source_root = source_root.resolve()
    project = tomllib.loads((source_root / "pyproject.toml").read_text()).get("project")
    if not isinstance(project, dict) or not isinstance(project.get("version"), str):
        _fail("pyproject.toml must declare project.version as a string")
    if not isinstance(project.get("name"), str):
        _fail("pyproject.toml must declare project.name as a string")
    python_raw = cast("str", project["version"])
    project_identity = _python_identity(python_raw, source="project.version")
    version = Version(python_raw)

    if tag is not None:
        if not tag.startswith(tag_prefix) or len(tag) == len(tag_prefix):
            _fail(f"Release tag must start with {tag_prefix}, found {tag!r}")
        tag_raw = tag.removeprefix(tag_prefix)
        tag_identity = _python_identity(tag_raw, source="release tag")
        if tag_identity != project_identity:
            _fail(f"Release tag version {tag_raw!r} does not match project version {python_raw!r}")

    if wheel_dir is not None:
        _check_wheels(
            wheel_dir.resolve(),
            distribution=cast("str", project["name"]),
            version=version,
            expected_files=expected_files,
        )
    return version


def wheel_filename(distribution: str, version: Version) -> str:
    """Name the one universal wheel of ``distribution`` (PEP 427, normalized name)."""
    return f"{re.sub(r'[-_.]+', '_', distribution).lower()}-{version}-py3-none-any.whl"


def _python_identity(raw: str, *, source: str) -> ReleaseIdentity:
    try:
        return python_release_identity(raw, source=source)
    except ReleaseVersionSyntaxError as exc:
        raise ReleaseVersionError(str(exc)) from exc


def _check_wheels(
    wheel_dir: Path,
    *,
    distribution: str,
    version: Version,
    expected_files: Callable[[str], set[str]] | None,
) -> None:
    expected = (
        {wheel_filename(distribution, version)}
        if expected_files is None
        else expected_files(str(version))
    )
    actual = {
        path.name for path in wheel_dir.iterdir() if path.is_file() and path.name != ".gitignore"
    }
    if actual != expected:
        _fail(
            "Release directory must contain exactly one universal wheel and no sdist "
            "(or exactly the files the release names): "
            f"expected {sorted(expected)}, found {sorted(actual)}"
        )
    for filename in sorted(expected):
        if not filename.endswith(".whl"):
            continue
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


def _evaluator_files() -> Callable[[str], set[str]]:
    return release_names


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag")
    parser.add_argument("--wheel-dir", type=Path)
    parser.add_argument("--source-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--tag-prefix", default=DEFAULT_TAG_PREFIX)
    parser.add_argument(
        "--evaluator-wheels",
        action="store_true",
        help="the release is vibesys-evaluator's platform wheels and sdist (evaluator_wheels.py)",
    )
    parser.add_argument(
        "--go-module-version",
        action="store_true",
        help="print the version as a Go module semantic version (0.2.0rc1 -> 0.2.0-rc.1)",
    )
    return parser.parse_args()


def main() -> int:
    """Validate CLI inputs and print the agreed version."""
    args = _parse_args()
    try:
        version = check_release_version(
            args.tag,
            source_root=args.source_root,
            wheel_dir=args.wheel_dir,
            tag_prefix=args.tag_prefix,
            expected_files=_evaluator_files() if args.evaluator_wheels else None,
        )
    except (OSError, ValueError, ReleaseVersionError) as exc:
        print(f"release version check failed: {exc}", file=sys.stderr)
        return 1
    if args.go_module_version:
        print(go_module_version(_python_identity(str(version), source="project.version")))
    else:
        print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
