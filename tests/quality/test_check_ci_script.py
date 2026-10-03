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
