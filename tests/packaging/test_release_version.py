"""Release version and aggregate artifact contracts."""

from __future__ import annotations

import re
import zipfile
from typing import TYPE_CHECKING

import pytest
from check_release_version import (
    ReleaseVersionError,
    check_release_version,
)
from hypothesis import given
from hypothesis import strategies as st
from packaging.version import Version
from release_versions import go_module_version, python_release_identity

if TYPE_CHECKING:
    from pathlib import Path


def _write_project(root: Path, *, python_version: str = "0.1.0") -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "vibesys"\nversion = "{python_version}"\n'
    )


def _write_wheels(root: Path, version: str = "0.1.0") -> None:
    root.mkdir(parents=True)
    metadata = f"Metadata-Version: 2.4\nName: vibesys\nVersion: {version}\n\n"
    wheel = root / f"vibesys-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(f"vibesys-{version}.dist-info/METADATA", metadata)


def test_check_release_version_accepts_matching_project_tag_and_wheel(tmp_path: Path) -> None:
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
    ("python_version", "tag"),
    [
        ("1.4.0", "refs/tags/v1.4.0"),
        ("0.2.0rc1", "refs/tags/v0.2.0rc1"),
    ],
)
def test_check_release_version_accepts_canonical_stable_and_rc_spellings(
    tmp_path: Path,
    python_version: str,
    tag: str,
) -> None:
    _write_project(tmp_path, python_version=python_version)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir, version=python_version)

    assert check_release_version(tag, source_root=tmp_path, wheel_dir=wheel_dir) == Version(
        python_version
    )


@pytest.mark.parametrize(
    ("python_version", "tag", "message"),
    [
        ("not-a-version", None, "PEP 440"),
        ("0.1.0", "refs/tags/0.1.0", "refs/tags/v"),
        ("0.1.0", "v0.1.0", "refs/tags/v"),
        ("0.1.0", "refs/heads/v0.1.0", "refs/tags/v"),
        ("0.1", "refs/tags/v0.1", "X.Y.Z"),
        ("0.1.0", "refs/tags/v0.2.0", "does not match"),
        ("0.2.0-rc.1", None, "canonical PEP 440"),
    ],
)
def test_check_release_version_rejects_invalid_or_mismatched_versions(
    tmp_path: Path,
    python_version: str,
    tag: str | None,
    message: str,
) -> None:
    _write_project(tmp_path, python_version=python_version)

    with pytest.raises(ReleaseVersionError, match=message):
        check_release_version(tag, source_root=tmp_path)


def test_check_release_version_rejects_normalized_but_noncanonical_versions(tmp_path: Path) -> None:
    _write_project(tmp_path, python_version="01.0")

    with pytest.raises(ReleaseVersionError, match="canonical"):
        check_release_version(None, source_root=tmp_path)


@pytest.mark.parametrize("mutation", ["missing", "extra", "sdist"])
def test_check_release_version_requires_exact_universal_wheel(
    tmp_path: Path,
    mutation: str,
) -> None:
    _write_project(tmp_path)
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir)
    if mutation == "missing":
        next(wheel_dir.glob("*.whl")).unlink()
    elif mutation == "extra":
        (wheel_dir / "unreviewed-0.1.0-py3-none-any.whl").write_bytes(b"wheel")
    else:
        (wheel_dir / "vibesys-0.1.0.tar.gz").write_bytes(b"sdist")

    with pytest.raises(ReleaseVersionError, match="exactly one universal wheel"):
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


def _write_evaluator_project(root: Path, *, python_version: str) -> None:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "vibesys-evaluator"\nversion = "{python_version}"\n'
    )


@pytest.mark.parametrize("python_version", ["0.1.0", "0.1.0rc1"])
def test_evaluator_release_uses_its_tag_prefix_and_normalized_wheel_name(
    tmp_path: Path, python_version: str
) -> None:
    _write_evaluator_project(tmp_path, python_version=python_version)
    wheel_dir = tmp_path / "dist"
    wheel_dir.mkdir()
    wheel = wheel_dir / f"vibesys_evaluator-{python_version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(
            f"vibesys_evaluator-{python_version}.dist-info/METADATA",
            f"Metadata-Version: 2.4\nName: vibesys-evaluator\nVersion: {python_version}\n\n",
        )

    version = check_release_version(
        f"refs/tags/evaluator-v{python_version}",
        source_root=tmp_path,
        wheel_dir=wheel_dir,
        tag_prefix="refs/tags/evaluator-v",
    )

    assert version == Version(python_version)


@pytest.mark.parametrize(
    "tag",
    [
        # The main package's tag does not release the evaluator, nor the reverse.
        "refs/tags/v0.1.0",
        "refs/tags/cli-v0.1.0",
        "refs/tags/evaluator-v",
        "refs/tags/evaluator-v0.1.1",
        "refs/tags/evaluator-v0.1.0-rc.1",
    ],
)
def test_evaluator_release_rejects_other_tags(tmp_path: Path, tag: str) -> None:
    _write_evaluator_project(tmp_path, python_version="0.1.0")

    with pytest.raises(ReleaseVersionError):
        check_release_version(tag, source_root=tmp_path, tag_prefix="refs/tags/evaluator-v")


def test_evaluator_wheel_dir_rejects_the_unnormalized_wheel_name(tmp_path: Path) -> None:
    _write_evaluator_project(tmp_path, python_version="0.1.0")
    wheel_dir = tmp_path / "dist"
    _write_wheels(wheel_dir)

    with pytest.raises(
        ReleaseVersionError, match=re.escape("vibesys_evaluator-0.1.0-py3-none-any.whl")
    ):
        check_release_version(None, source_root=tmp_path, wheel_dir=wheel_dir)


@given(
    major=st.integers(min_value=0, max_value=10**6),
    minor=st.integers(min_value=0, max_value=10**6),
    patch=st.integers(min_value=0, max_value=10**6),
    rc=st.none() | st.integers(min_value=0, max_value=10**6),
)
def test_go_module_version_orders_and_spells_like_semver(
    major: int, minor: int, patch: int, rc: int | None
) -> None:
    raw = f"{major}.{minor}.{patch}" + ("" if rc is None else f"rc{rc}")
    go = go_module_version(python_release_identity(raw, source="test"))

    base = f"{major}.{minor}.{patch}"
    assert go == (base if rc is None else f"{base}-rc.{rc}")
    # A Go module tag must be valid semver: the core is the PEP 440 release, a candidate is a
    # dot-separated pre-release (so rc.10 orders after rc.9, as rc10 does after rc9 in PEP 440).
    assert re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-rc\.(0|[1-9]\d*))?", go)
