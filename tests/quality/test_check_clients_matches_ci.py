"""`scripts/check_clients.sh` runs what CI runs for a change under ``clients/``.

The local command lists the check groups it runs and the CI groups it leaves out (with a reason).
CI's client jobs run named repoctl groups from ``.repoctl/checks.toml``. This test derives the
groups the ``clients-*`` jobs of the workflow run and requires each to be in exactly one of the two
lists, and every listed group to be defined, so a group added to CI cannot be forgotten locally.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"
CHECKS = REPO_ROOT / ".repoctl" / "checks.toml"
SCRIPT = REPO_ROOT / "scripts" / "check_clients.sh"
GROUP_FLAG = re.compile(r"run-checks\s+--group\s+([a-z0-9_]+)")


def _array(name: str) -> list[str]:
    match = re.search(rf"^{name}=\(([^)]*)\)", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{SCRIPT.name} declares no {name} array"
    return match.group(1).split()


def _client_ci_groups() -> set[str]:
    jobs: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    groups: set[str] = set()
    for name, job in jobs.items():
        if not name.startswith("clients-"):
            continue
        for step in job.get("steps", []):
            groups.update(GROUP_FLAG.findall(str(step.get("run", ""))))
    return groups


def test_every_group_ci_runs_for_clients_is_run_or_skipped_with_a_reason() -> None:
    ci = _client_ci_groups()
    assert ci, "found no client check group in the workflow; the job condition changed"
    parity, skipped = set(_array("parity_groups")), set(_array("skipped_groups"))
    assert not parity & skipped, f"in both lists: {sorted(parity & skipped)}"
    assert ci - parity - skipped == set(), (
        f"CI runs {sorted(ci - parity - skipped)} for clients/ but {SCRIPT.name} neither runs "
        "nor lists it as skipped"
    )
    assert skipped - ci == set(), f"skipped groups CI no longer runs: {sorted(skipped - ci)}"


def test_every_listed_group_is_defined_in_the_check_policy() -> None:
    defined = {
        group["name"] for group in tomllib.loads(CHECKS.read_text(encoding="utf-8"))["check_groups"]
    }
    listed = set(_array("parity_groups")) | set(_array("skipped_groups"))
    assert listed - defined == set(), f"undefined check groups: {sorted(listed - defined)}"
