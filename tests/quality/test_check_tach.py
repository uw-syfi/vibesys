"""The tach wrapper enforces edges and interfaces whether or not a library is packaged.

One throwaway repository holds a pair of libraries per case, and one run of
`scripts/check_tach.py` through its CLI reports on all of them. A library may carry its own
`pyproject.toml` (a uv workspace member) or not; a planted import across an
undeclared edge, or around a library's `api`, must be reported in every combination, and a
declared import through the `api` must not be.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

if TYPE_CHECKING:
    from collections.abc import Sequence

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_tach.py"

# One importer/exporter pair per (behaviour, packaging of the importer, packaging of the exporter).
PACKAGING = list(itertools.product((False, True), repeat=2))
BEHAVIOURS = {
    # name: (what the importer imports from its exporter, what its module declares)
    "declared": ("from {b}.api import VALUE\n", '["{b}"]'),
    "undeclared": ("from {b}.api import VALUE\n", "[]"),
    "around": ("from {b}.internal import VALUE\n", '["{b}"]'),
}
CASES = [(behaviour, packaged) for behaviour in BEHAVIOURS for packaged in PACKAGING]


def _names(behaviour: str, packaged: Sequence[bool]) -> tuple[str, str]:
    suffix = f"{behaviour}_{int(packaged[0])}{int(packaged[1])}"
    return f"imp_{suffix}", f"exp_{suffix}"


def build_repository(root: Path) -> None:
    """Write one repository holding a pair of libraries for every case.

    ``tach`` reports every violation of a repository in one run, so all cases share one run
    and each is read from its own library's lines of the report.
    """
    config = 'exclude = ["**/tests", "**/__pycache__"]\nsource_roots = [\n'
    modules = ""
    for behaviour, packaged in CASES:
        importer, exporter = _names(behaviour, packaged)
        imports, depends_on = BEHAVIOURS[behaviour]
        for name, is_packaged in zip((importer, exporter), packaged, strict=True):
            library = root / "libs" / name
            package = library / "src" / name
            package.mkdir(parents=True)
            (package / "__init__.py").write_text("", encoding="utf-8")
            if is_packaged:
                (library / "pyproject.toml").write_text(
                    f'[project]\nname = "{name}"\nversion = "0.1.0"\n', encoding="utf-8"
                )
            config += f'    "libs/{name}/src",\n'
        (root / "libs" / exporter / "src" / exporter / "api.py").write_text(
            "VALUE = 1\n", encoding="utf-8"
        )
        (root / "libs" / exporter / "src" / exporter / "internal.py").write_text(
            "VALUE = 2\n", encoding="utf-8"
        )
        (root / "libs" / importer / "src" / importer / "use.py").write_text(
            imports.format(b=exporter), encoding="utf-8"
        )
        modules += (
            f'[[modules]]\npath = "{importer}"\ndepends_on = {depends_on.format(b=exporter)}\n\n'
            f'[[modules]]\npath = "{exporter}"\ndepends_on = []\n\n'
            f'[[interfaces]]\nexpose = ["api", "api\\\\..*"]\nfrom = ["{exporter}"]\n\n'
        )
    (root / "tach.toml").write_text(config + "]\n\n" + modules, encoding="utf-8")


@pytest.fixture
def report(tmp_path_factory: pytest.TempPathFactory) -> tuple[int, str]:
    """The wrapper's one run over every case: its exit code and its output."""
    root = tmp_path_factory.mktemp("tach-cases")
    build_repository(root)
    result = run_test_command(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def _lines_of(output: str, library: str) -> list[str]:
    return [line for line in output.splitlines() if library in line]


def test_the_wrapper_enforces_edges_and_interfaces_whether_or_not_a_library_is_packaged(
    report: tuple[int, str],
) -> None:
    returncode, output = report
    wrong: list[str] = []
    for behaviour, packaged in CASES:
        importer, exporter = _names(behaviour, packaged)
        lines = _lines_of(output, f"/{importer}/")
        if behaviour == "declared":
            reported = bool(lines)
        elif behaviour == "undeclared":
            reported = any(importer in line and exporter in line for line in lines)
        else:
            reported = any(f"{exporter}.internal" in line for line in lines)
        if reported != (behaviour != "declared"):
            wrong.append(f"{behaviour} {importer}: {lines}")

    assert returncode != 0
    assert not wrong, "\n".join(wrong)
