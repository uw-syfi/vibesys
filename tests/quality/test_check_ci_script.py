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


def test_policy_commands_never_use_a_bare_python_from_path() -> None:
    # A bare `python3` resolves to whatever is first on PATH (the system 3.11 on some hosts), which
    # cannot parse sources the project's pinned Python can. Python commands go through `uv run`.
    bare = [
        command
        for group in POLICY["check_groups"]
        for command in group.get("commands", [])
        if Path(command[0]).name in {"python", "python3"}
        or re.fullmatch(r"python[\d.]*", command[0])
    ]

    assert bare == [], f"run these through `uv run python`: {bare}"


def test_check_shell_scripts_never_invoke_a_bare_python() -> None:
    offenders = [
        f"{script.name}:{number}: {line.strip()}"
        for script in sorted((REPO_ROOT / "scripts").glob("*.sh"))
        for number, line in enumerate(script.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"(^|[\s;&|(])python[\d.]*\s", line)
        and not line.lstrip().startswith("#")
        and "uv run" not in line
    ]

    assert offenders == [], offenders


def coverage_groups() -> list[str]:
    match = re.search(r"^COVERAGE_GROUPS=\(([^)]*)\)", SCRIPT, re.MULTILINE)
    assert match, "check_ci.sh must declare COVERAGE_GROUPS=(...)"
    return match.group(1).split()


def test_patch_coverage_gate_runs_the_groups_ci_runs_in_the_order_ci_runs_them() -> None:
    """Regression: check_ci.sh skipped the patch-coverage gate, so it failed only in CI.

    The script names the CI groups that produce coverage data (the test run, the combine step)
    and apply the gate, in workflow order, and carries none of their thresholds or commands.
    """
    policy_groups = {g["name"] for g in POLICY["check_groups"]}
    ci_order = re.findall(r"repoctl run-checks --group (\w+)", WORKFLOW)
    groups = coverage_groups()

    assert groups == ["python_ci_tests_shard", "python_ci_coverage", "patch_coverage"]
    assert set(groups) <= policy_groups
    assert [ci_order.index(name) for name in groups] == sorted(ci_order.index(n) for n in groups)
    assert "fail-under" not in SCRIPT
    assert "diff-cover" not in SCRIPT
