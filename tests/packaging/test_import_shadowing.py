"""Guard against tests/packaging or packaging/ shadowing the PyPA `packaging` library."""

from pathlib import Path

import packaging.version

import packaging

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_import_packaging_resolves_to_pypa_library() -> None:
    assert packaging.version.Version("1.0") < packaging.version.Version("2.0")
    # A repo-local shadow would live at <repo>/packaging or <repo>/tests/packaging.
    package_dir = Path(packaging.__file__).resolve().parent
    assert package_dir not in {REPO_ROOT / "packaging", REPO_ROOT / "tests" / "packaging"}


def test_packaging_directories_are_not_packages() -> None:
    assert not (REPO_ROOT / "tests" / "packaging" / "__init__.py").exists()
    assert not (REPO_ROOT / "packaging" / "__init__.py").exists()
