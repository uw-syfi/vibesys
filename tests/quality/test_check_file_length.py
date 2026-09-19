"""Behavioral tests for the Python file-length ratchet.

Every case drives `scripts/check_file_length.py` through its CLI against a
throwaway fixture tree, so the assertions cover the contract CI depends on
(exit code plus the file named in the output) rather than internal helpers.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_file_length.py"


def write_module(root: Path, relative: str, lines: int) -> None:
    """Create a Python file at ``relative`` with exactly ``lines`` physical lines."""
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(f"x = {index}" for index in range(lines)) + "\n", encoding="utf-8")


def write_config(root: Path, max_lines: int, allowlist: dict[str, int]) -> None:
    """Write a fixture `pyproject.toml` carrying a `[tool.vibesys.file_length]` section."""
    entries = "\n".join(f'"{path}" = {count}' for path, count in allowlist.items())
    (root / "pyproject.toml").write_text(
        "[tool.vibesys.file_length]\n"
        f"max_lines = {max_lines}\n"
        'roots = ["src"]\n'
        "\n[tool.vibesys.file_length.allowlist]\n"
        f"{entries}\n",
        encoding="utf-8",
    )


def run_check(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the checker against the fixture tree at ``root``."""
    return subprocess.run(  # noqa: S603  # tracked: #288
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


def test_passes_when_every_file_is_under_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/small.py", 50)

    result = run_check(tmp_path)

    assert result.returncode == 0
    assert "within the 100-line ceiling" in result.stdout


def test_fails_when_a_new_file_exceeds_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/small.py", 50)
    write_module(tmp_path, "src/huge.py", 150)

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "src/huge.py: 150 lines > 100" in result.stdout
    assert "src/small.py" not in result.stdout


def test_fails_when_an_allowlisted_file_grows(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 151)

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "src/legacy.py: 151 lines > 150 (recorded)" in result.stdout


def test_passes_and_suggests_tightening_when_an_allowlisted_file_shrinks(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 120)

    result = run_check(tmp_path)

    assert result.returncode == 0
    assert "src/legacy.py: 150 -> 120" in result.stdout


def test_fails_when_an_allowlisted_file_drops_under_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 90)

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "Stale" in result.stdout
    assert "src/legacy.py: now 90 lines" in result.stdout


def test_fails_when_an_allowlisted_file_disappears(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/gone.py": 150})
    write_module(tmp_path, "src/kept.py", 10)

    result = run_check(tmp_path)

    assert result.returncode == 1
    assert "src/gone.py: no longer exists" in result.stdout


def test_skips_test_modules_and_caches(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/pkg/tests/test_big.py", 500)
    write_module(tmp_path, "src/pkg/__pycache__/cached.py", 500)

    result = run_check(tmp_path)

    assert result.returncode == 0


def test_reports_a_tool_error_when_the_config_section_is_missing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.other]\nkey = 1\n", encoding="utf-8")

    result = run_check(tmp_path)

    assert result.returncode == 2
    assert "missing [tool.vibesys.file_length]" in result.stderr
