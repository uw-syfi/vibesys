"""The type check targets Linux, the platform CI runs, and macOS, which the product also runs on.

``ty check`` with no platform uses the host. On macOS that reports Linux-only
APIs (``os.waitid``, ``signal.sigtimedwait``, ``os.pidfd_open``) that the
Linux typecheck job never sees, and on Linux it never reports an API macOS
lacks. The script is the only command that job runs, so it checks both
platforms: typeshed marks which names each platform lacks. The macOS pass covers
the source roots a macOS host loads, not the tests.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

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


def test_the_macos_check_covers_every_source_root_the_type_checker_is_configured_with() -> None:
    """A new library under ``libs`` joins the macOS pass without anyone listing it there."""
    (darwin,) = (line for line in _commands() if "--python-platform darwin" in line)
    globs = darwin.split("--python-platform darwin", 1)[1].split(";")[0].split()
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    included = config["tool"]["ty"]["src"]["include"]
    roots = [entry for entry in included if entry == "src" or entry.endswith("/src")]

    assert globs == ["src", "libs/*/src", "sdk/*/src"]
    assert roots, "the ty configuration lists no source roots"
    assert all(any(fnmatch.fnmatchcase(root, glob) for glob in globs) for root in roots), roots
