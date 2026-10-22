"""CI and local partial-suite reports share one coverage policy."""

import json
from pathlib import Path

import pytest
from scripts.check_coverage_floor import main


@pytest.mark.parametrize(("percent", "expected"), [(0, 1), (37.9, 1), (40, 0), (100, 0)])
def test_partial_report_uses_configured_floor(
    tmp_path: Path, percent: float, expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.vibesys.per_module_coverage]\nfloor = 40\nallowlist = ["other.py"]\n')
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "src/host.py": {"summary": {"num_statements": 10, "percent_covered": percent}}
                }
            }
        )
    )
    assert main([str(report), "--pyproject", str(config), "--module", "src/host.py"]) == expected
    output = capsys.readouterr()
    assert "src/host.py" in output.out + output.err
    assert "Stale" not in output.err


@pytest.mark.parametrize(
    ("modules", "expected"), [([], 1), (["missing.py"], 1), (["src/host.py"], 0)]
)
def test_partial_scope_preserves_full_report_checks(
    tmp_path: Path, modules: list[str], expected: int, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text('[tool.vibesys.per_module_coverage]\nfloor = 63\nallowlist = ["other.py"]\n')
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "src/host.py": {"summary": {"num_statements": 10, "percent_covered": 64}},
                    "unrelated.py": {"summary": {"num_statements": 10, "percent_covered": 0}},
                }
            }
        )
    )
    args = [str(report), "--pyproject", str(config)]
    for module in modules:
        args.extend(["--module", module])
    assert main(args) == expected
    output = capsys.readouterr()
    if modules == ["missing.py"]:
        assert "missing.py: missing from coverage report" in output.err
    if not modules:
        assert "Stale" in output.err
        assert "unrelated.py" in output.err


@pytest.mark.parametrize(
    ("allowlist", "statements", "expected"),
    [(["src/host.py"], 10, 0), (["src/"], 10, 0), ([], 0, 0), ([], 10, 1)],
)
def test_partial_reports_preserve_exemptions(
    tmp_path: Path, allowlist: list[str], statements: int, expected: int
) -> None:
    config = tmp_path / "pyproject.toml"
    config.write_text(
        "[tool.vibesys.per_module_coverage]\nfloor = 75\nallowlist = "
        + json.dumps(allowlist)
        + "\n"
    )
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "files": {
                    "src/host.py": {"summary": {"num_statements": statements, "percent_covered": 0}}
                }
            }
        )
    )
    assert main([str(report), "--pyproject", str(config), "--module", "src/host.py"]) == expected
