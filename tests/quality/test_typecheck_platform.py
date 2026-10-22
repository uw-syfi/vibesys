"""The type check targets Linux, which is the platform CI runs and the product ships on.

``ty check`` with no platform uses the host. On macOS that reports Linux-only
APIs (``os.waitid``, ``signal.sigtimedwait``, ``os.pidfd_open``) that the
Linux typecheck job never sees. The script is the only command that job runs.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "check_types.sh"
CHECKS = ROOT / ".repoctl" / "checks.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _commands(source: str) -> list[str]:
    return [
        line.strip()
        for line in source.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_the_typecheck_script_pins_the_linux_platform() -> None:
    assert _commands(SCRIPT.read_text(encoding="utf-8")) == [
        "set -euo pipefail",
        "uv run ty check --python-platform linux",
    ]


def test_ci_typecheck_runs_that_script_and_no_other_ty_command() -> None:
    checks = tomllib.loads(CHECKS.read_text(encoding="utf-8"))
    group = next(item for item in checks["check_groups"] if item["name"] == "python_types")
    loaded: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = loaded["jobs"]["typecheck"]["steps"]
    runs = [str(step.get("run", "")) for step in steps]

    assert group["commands"] == [["./scripts/check_types.sh"]]
    assert any("run-checks --group python_types" in run for run in runs)
    assert all("ty check" not in run for run in runs)
