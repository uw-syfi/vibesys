"""Behavioral tests for the Python file-length ratchet.

Cases pin the exit code and offending path that CI depends on through the
public measurement and reporting API. Fixture trees inject a tracked listing
so throwaway directories do not need a git repository.
"""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest
from scripts.check_file_length import (
    Config,
    ConfigError,
    check_allowlist,
    check_measured_files,
    load_config,
    measure,
    report,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def _python_names(root: Path) -> tuple[str, ...]:
    return tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*.py") if path.is_file())
    )


def run_check(root: Path) -> tuple[int, str]:
    """Run the ratchet against the fixture tree at ``root`` using an injected listing."""
    config = load_config(root / "pyproject.toml")
    measured = measure(root, config.roots, config.generated, names=_python_names(root))
    failures = check_measured_files(measured, config)
    stale, tightenable = check_allowlist(measured, config)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = report(failures, stale, tightenable, config.max_lines)
    return code, buffer.getvalue()


def test_passes_when_every_file_is_under_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/small.py", 50)

    code, stdout = run_check(tmp_path)

    assert code == 0
    assert "within the 100-line ceiling" in stdout


def test_fails_when_a_new_file_exceeds_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/small.py", 50)
    write_module(tmp_path, "src/huge.py", 150)

    code, stdout = run_check(tmp_path)

    assert code == 1
    assert "src/huge.py: 150 lines > 100" in stdout
    assert "src/small.py" not in stdout


def test_fails_when_an_allowlisted_file_grows(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 151)

    code, stdout = run_check(tmp_path)

    assert code == 1
    assert "src/legacy.py: 151 lines > 150 (recorded)" in stdout


def test_passes_and_suggests_tightening_when_an_allowlisted_file_shrinks(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 120)

    code, stdout = run_check(tmp_path)

    assert code == 0
    assert "src/legacy.py: 150 -> 120" in stdout


def test_fails_when_an_allowlisted_file_drops_under_the_ceiling(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/legacy.py": 150})
    write_module(tmp_path, "src/legacy.py", 90)

    code, stdout = run_check(tmp_path)

    assert code == 1
    assert "Stale" in stdout
    assert "src/legacy.py: now 90 lines" in stdout


def test_fails_when_an_allowlisted_file_disappears(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={"src/gone.py": 150})
    write_module(tmp_path, "src/kept.py", 10)

    code, stdout = run_check(tmp_path)

    assert code == 1
    assert "src/gone.py: no longer exists" in stdout


def test_skips_test_modules_and_caches(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    write_module(tmp_path, "src/pkg/tests/test_big.py", 500)
    write_module(tmp_path, "src/pkg/__pycache__/cached.py", 500)

    code, _stdout = run_check(tmp_path)

    assert code == 0


def test_reports_a_tool_error_when_the_config_section_is_missing(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.other]\nkey = 1\n", encoding="utf-8")

    with pytest.raises(ConfigError, match=r"missing \[tool.vibesys.file_length\]"):
        load_config(tmp_path / "pyproject.toml")


def test_generated_directory_exclusion_does_not_hide_handwritten_siblings(tmp_path: Path) -> None:
    write_module(tmp_path, "src/pkg/_generated/binding.py", 150)
    write_module(tmp_path, "src/pkg/_generated/nested/binding.py", 150)
    write_module(tmp_path, "src/pkg/api.py", 150)
    write_module(tmp_path, "src/pkg/_generated_other/handwritten.py", 150)
    measured = measure(
        tmp_path,
        ("src",),
        ("src/pkg/_generated",),
        names=_python_names(tmp_path),
    )
    failures = check_measured_files(measured, Config(100, ("src",), {}))
    assert len(failures) == 2
    assert any("src/pkg/api.py" in failure for failure in failures)
    assert any("src/pkg/_generated_other/handwritten.py" in failure for failure in failures)


def test_generated_directory_globs_are_loaded_from_config(tmp_path: Path) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        config_path.read_text().replace(
            'roots = ["src"]',
            'roots = ["src"]\ngenerated = ["src/*/_generated"]',
        )
    )
    write_module(tmp_path, "src/pkg/_generated/binding.py", 150)
    config = load_config(config_path)
    assert measure(tmp_path, config.roots, config.generated, names=_python_names(tmp_path)) == {}


@pytest.mark.parametrize("value", ['"src/_generated"', "[1]", '["/absolute"]', '["../outside"]'])
def test_generated_exclusions_reject_invalid_directory_globs(tmp_path: Path, value: str) -> None:
    write_config(tmp_path, max_lines=100, allowlist={})
    config_path = tmp_path / "pyproject.toml"
    config_path.write_text(
        config_path.read_text().replace(
            'roots = ["src"]',
            f'roots = ["src"]\ngenerated = {value}',
        )
    )
    with pytest.raises(ConfigError, match=r"file_length\.generated"):
        load_config(config_path)


def test_repository_measure_uses_git_ls_files() -> None:
    """Smoke the production path against this checkout."""
    measured = measure(REPO_ROOT, ("scripts",))
    assert "scripts/check_file_length.py" in measured
    assert all(
        not relative.startswith("examples/microservices/repositories/deathstarbench/")
        for relative in measured
    )
