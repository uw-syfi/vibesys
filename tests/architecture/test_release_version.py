"""Release version and aggregate artifact contracts."""

from __future__ import annotations

import json
import zipfile
from typing import TYPE_CHECKING

import pytest
from check_release_version import (
    ReleaseVersionError,
    check_release_version,
)
from packaging.version import Version
from wheel_targets import TARGETS

if TYPE_CHECKING:
    from pathlib import Path


def _write_project(
    root: Path, *, python_version: str = "0.1.0", tui_version: str = "0.1.0"
) -> None:
    (root / "clients" / "tui").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "vibesys"\nversion = "{python_version}"\n'
    )
    (root / "clients" / "tui" / "package.json").write_text(
        json.dumps({"name": "@vibesys/tui", "version": tui_version})
    )


def _write_wheels(root: Path, version: str = "0.1.0") -> None:
    root.mkdir(parents=True)
    metadata = f"Metadata-Version: 2.4\nName: vibesys\nVersion: {version}\n\n"
    for target in TARGETS.values():
        wheel = root / f"vibesys-{version}-py3-none-{target.wheel_platform}.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(f"vibesys-{version}.dist-info/METADATA", metadata)


def test_check_release_version_accepts_matching_project_tag_and_four_wheels(tmp_path: Path) -> None:
    _write_project(tmp_path)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir)

    version = check_release_version(
        "refs/tags/v0.1.0",
        source_root=tmp_path,
        wheel_dir=wheel_dir,
    )

    assert version == Version("0.1.0")


@pytest.mark.parametrize(
    ("python_version", "tui_version", "tag"),
    [
        ("1.4.0", "1.4.0", "refs/tags/v1.4.0"),
        ("0.2.0rc1", "0.2.0-rc.1", "refs/tags/v0.2.0rc1"),
    ],
)
def test_check_release_version_accepts_canonical_stable_and_rc_spellings(
    tmp_path: Path,
    python_version: str,
    tui_version: str,
    tag: str,
) -> None:
    _write_project(tmp_path, python_version=python_version, tui_version=tui_version)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir, version=python_version)

    assert check_release_version(tag, source_root=tmp_path, wheel_dir=wheel_dir) == Version(
        python_version
    )


@pytest.mark.parametrize(
    ("python_version", "tui_version", "tag", "message"),
    [
        ("not-a-version", "not-a-version", None, "PEP 440"),
        ("0.1.0", "0.2.0", None, "TUI version"),
        ("0.1.0", "0.1.0", "refs/tags/0.1.0", "refs/tags/v"),
        ("0.1.0", "0.1.0", "v0.1.0", "refs/tags/v"),
        ("0.1.0", "0.1.0", "refs/heads/v0.1.0", "refs/tags/v"),
        ("0.1", "0.1", "refs/tags/v0.1", "X.Y.Z"),
        ("0.1.0", "0.1.0", "refs/tags/v0.2.0", "does not match"),
        ("0.2.0rc1", "0.2.0rc1", None, "canonical npm SemVer"),
        ("0.2.0-rc.1", "0.2.0-rc.1", None, "canonical PEP 440"),
    ],
)
def test_check_release_version_rejects_invalid_or_mismatched_versions(
    tmp_path: Path,
    python_version: str,
    tui_version: str,
    tag: str | None,
    message: str,
) -> None:
    _write_project(tmp_path, python_version=python_version, tui_version=tui_version)

    with pytest.raises(ReleaseVersionError, match=message):
        check_release_version(tag, source_root=tmp_path)


def test_check_release_version_rejects_normalized_but_noncanonical_versions(tmp_path: Path) -> None:
    _write_project(tmp_path, python_version="01.0", tui_version="01.0")

    with pytest.raises(ReleaseVersionError, match="canonical"):
        check_release_version(None, source_root=tmp_path)


@pytest.mark.parametrize("mutation", ["missing", "extra", "sdist"])
def test_check_release_version_requires_exact_four_wheel_aggregate(
    tmp_path: Path,
    mutation: str,
) -> None:
    _write_project(tmp_path)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir)
    if mutation == "missing":
        next(wheel_dir.glob("*x86_64.whl")).unlink()
    elif mutation == "extra":
        (wheel_dir / "unreviewed-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    else:
        (wheel_dir / "vibesys-0.1.0.tar.gz").write_bytes(b"sdist")

    with pytest.raises(ReleaseVersionError, match="exactly the four expected wheels"):
        check_release_version(None, source_root=tmp_path, wheel_dir=wheel_dir)


def test_check_release_version_rejects_wheel_metadata_version_drift(tmp_path: Path) -> None:
    _write_project(tmp_path)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir, version="0.1.0")
    wheel = next(wheel_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            "vibesys-0.1.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: vibesys\nVersion: 0.2.0\n\n",
        )

    with pytest.raises(ReleaseVersionError, match="METADATA version"):
        check_release_version(None, source_root=tmp_path, wheel_dir=wheel_dir)
