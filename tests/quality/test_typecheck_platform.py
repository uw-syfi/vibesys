"""The type check targets Linux, the platform CI runs, and macOS, which the product also runs on.

``ty check`` with no platform uses the host. On macOS that reports Linux-only
APIs (``os.waitid``, ``signal.sigtimedwait``, ``os.pidfd_open``) that the
Linux typecheck job never sees, and on Linux it never reports an API macOS
lacks. The script is the only command that job runs, so it checks both
platforms: typeshed marks which names each platform lacks, and no CI job runs on
macOS. The real tiers drive Linux systems and are the only code left out.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKS = ROOT / ".repoctl" / "checks.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"
SCRIPT = ROOT / "scripts" / "check_types.sh"


def test_ci_typecheck_runs_that_script_and_no_other_ty_command() -> None:
    checks = tomllib.loads(CHECKS.read_text(encoding="utf-8"))
    group = next(item for item in checks["check_groups"] if item["name"] == "python_types")
    loaded: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = loaded["jobs"]["typecheck"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]

    assert group["commands"] == [["./scripts/check_types.sh"]]
    assert any("run-checks --group python_types" in run for run in runs)
    assert all("ty check" not in run for run in runs)


def _commands() -> list[str]:
    """Each ``ty check`` invocation in the script, continuation lines joined."""
    text = SCRIPT.read_text(encoding="utf-8").replace("\\\n", " ")
    return [line.strip() for line in text.splitlines() if "ty check" in line]


def test_the_type_check_covers_both_platforms_the_product_runs_on() -> None:
    platforms = [re.search(r"--python-platform (\w+)", line) for line in _commands()]

    assert [match.group(1) for match in platforms if match] == ["linux", "darwin"]


def test_the_macos_check_leaves_out_exactly_the_real_tiers(request: pytest.FixtureRequest) -> None:
    (darwin,) = (line for line in _commands() if "--python-platform darwin" in line)

    excluded = re.findall(r"--exclude ([\w./-]+)", darwin)

    assert sorted(excluded) == sorted(request.config.getini("sim_real_tiers"))
