"""Keep `scripts/check_ci.sh` a thin runner over the policy CI reads.

The script must not carry its own command list: it names check groups and
hands them to repoctl, which reads `.repoctl/checks.toml`. These tests fail
when the script names a group CI does not run, or stops delegating to repoctl.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = (REPO_ROOT / "scripts" / "check_ci.sh").read_text(encoding="utf-8")
WORKFLOW = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
POLICY = tomllib.loads((REPO_ROOT / ".repoctl" / "checks.toml").read_text(encoding="utf-8"))


def script_groups() -> list[str]:
    match = re.search(r"^CHECK_GROUPS=\(([^)]*)\)", SCRIPT, re.MULTILINE)
    assert match, "check_ci.sh must declare CHECK_GROUPS=(...)"
    return match.group(1).split()


def test_script_groups_are_python_groups_in_the_policy() -> None:
    policy_groups = {g["name"]: g for g in POLICY["check_groups"]}

    assert script_groups(), "no groups listed"
    for name in script_groups():
        assert name in policy_groups, f"{name} is not in .repoctl/checks.toml"
        assert policy_groups[name]["language"] == "python"


def test_script_groups_are_the_ones_ci_runs() -> None:
    ci_groups = set(re.findall(r"repoctl run-checks --group (\w+)", WORKFLOW))

    assert set(script_groups()) <= ci_groups


def test_script_delegates_to_repoctl_instead_of_listing_commands() -> None:
    assert "./support/repoctl/repoctl run-checks --group" in SCRIPT
    assert "uv run" not in SCRIPT


def policy_pytest_targets(group: str) -> set[str]:
    targets: set[str] = set()
    for g in POLICY["check_groups"]:
        if g["name"] == group:
            for command in g["commands"]:
                targets |= {a for a in command if a.startswith("tests/")}
    return targets


def test_script_runs_every_repo_policy_guard_directory() -> None:
    """Regression: check_ci.sh skipped tests/quality, so the real-API ratchet only failed in CI.

    Every directory that holds repo-policy guards (a baseline file or a `test_check_*` test)
    must be a pytest target of a group the script runs, so a new guard directory cannot be
    forgotten the way tests/quality was.
    """
    guard_dirs = {
        path.parent.relative_to(REPO_ROOT).as_posix()
        for pattern in ("*_baseline.jsonl", "test_check_*.py")
        for path in (REPO_ROOT / "tests").rglob(pattern)
    }
    assert "tests/quality" in guard_dirs

    covered: set[str] = set()
    for name in script_groups():
        covered |= policy_pytest_targets(name)

    missing = {d for d in guard_dirs if not any(d == t or d.startswith(t + "/") for t in covered)}
    assert not missing, f"check_ci.sh does not run guard tests in {sorted(missing)}"


def test_guard_group_runs_in_ci_next_to_quality_checks() -> None:
    quality_job = WORKFLOW.split("python_quality", 1)[1].split("typecheck:", 1)[0]
    assert "repoctl run-checks --group python_guards" in quality_job
