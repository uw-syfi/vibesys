"""The tach wrapper enforces edges and interfaces whether or not a library is packaged.

Each case builds a throwaway repository with two libraries and drives
`scripts/check_tach.py` through its CLI. A library may carry its own
`pyproject.toml` (a uv workspace member) or not; a planted import across an
undeclared edge, or around a library's `api`, must fail in every combination.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

if TYPE_CHECKING:
    import subprocess

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_tach.py"

TACH_CONFIG = r"""source_roots = ["libs/lib-a/src", "libs/lib-b/src"]
exclude = ["**/tests", "**/__pycache__"]

[[modules]]
path = "pkg_a"
depends_on = {a_depends_on}

[[modules]]
path = "pkg_b"
depends_on = []

[[interfaces]]
expose = ["api", "api\\..*"]
from = ["pkg_b"]
"""


def build_repository(
    root: Path, *, a_imports: str, a_depends_on: str, packaged: tuple[bool, bool]
) -> None:
    """Write a two-library repository where pkg_a's code is ``a_imports``."""
    for name, is_packaged in zip(("a", "b"), packaged, strict=True):
        package = root / "libs" / f"lib-{name}" / "src" / f"pkg_{name}"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        if is_packaged:
            (root / "libs" / f"lib-{name}" / "pyproject.toml").write_text(
                f'[project]\nname = "lib-{name}"\nversion = "0.1.0"\n', encoding="utf-8"
            )
    (root / "libs/lib-b/src/pkg_b/api.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "libs/lib-b/src/pkg_b/internal.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "libs/lib-a/src/pkg_a/use.py").write_text(a_imports, encoding="utf-8")
    (root / "tach.toml").write_text(TACH_CONFIG.format(a_depends_on=a_depends_on), encoding="utf-8")


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the wrapper against the fixture repository at ``root``."""
    return run_test_command(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


PACKAGING = list(itertools.product((False, True), repeat=2))


@pytest.mark.parametrize("packaged", PACKAGING)
def test_a_declared_import_through_the_api_passes(
    tmp_path: Path, packaged: tuple[bool, bool]
) -> None:
    build_repository(
        tmp_path,
        a_imports="from pkg_b.api import VALUE\n",
        a_depends_on='["pkg_b"]',
        packaged=packaged,
    )

    result = run_check(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("packaged", PACKAGING)
def test_an_undeclared_import_fails(tmp_path: Path, packaged: tuple[bool, bool]) -> None:
    build_repository(
        tmp_path,
        a_imports="from pkg_b.api import VALUE\n",
        a_depends_on="[]",
        packaged=packaged,
    )

    result = run_check(tmp_path)

    assert result.returncode != 0
    output = result.stdout + result.stderr
    assert "pkg_a" in output
    assert "pkg_b" in output


@pytest.mark.parametrize("packaged", PACKAGING)
def test_an_import_around_the_api_fails(tmp_path: Path, packaged: tuple[bool, bool]) -> None:
    build_repository(
        tmp_path,
        a_imports="from pkg_b.internal import VALUE\n",
        a_depends_on='["pkg_b"]',
        packaged=packaged,
    )

    result = run_check(tmp_path)

    assert result.returncode != 0
    assert "pkg_b.internal" in result.stdout + result.stderr
