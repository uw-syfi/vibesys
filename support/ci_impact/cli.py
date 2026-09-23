"""Human and CI command-line interface for component impact selection."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

from .git_diff import changed_paths
from .model import GIT, ROOT, SelectionError, load_policy
from .native_runner import run_native_targets
from .selector import select


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ci-impact", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Show checks affected by a Git change")
    plan.add_argument("--base", default="main", help="Base revision (default: main)")
    plan.add_argument("--head", default="HEAD", help="Head revision (default: HEAD)")
    plan.add_argument(
        "--event", choices=("pull_request", "merge_group", "push"), default="pull_request"
    )
    plan.add_argument("--github-output", type=Path, help=argparse.SUPPRESS)
    plan.add_argument("--json", action="store_true", help="Print the full machine-readable plan")
    explain = commands.add_parser("explain", help="Show checks affected by one repository path")
    explain.add_argument("path", help="Repository-relative path")
    explain.add_argument("--json", action="store_true", help="Print the full machine-readable plan")
    commands.add_parser("validate", help="Validate the graph and tracked-file ownership")
    run_native = commands.add_parser("run-native", help="Run selected Cargo and Go checks")
    run_native.add_argument(
        "--targets-json", required=True, help="JSON array from plan.native_targets"
    )
    return parser


def _display(plan: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps(plan, indent=2, sort_keys=True) + "\n")
        return
    selected = [name for name, enabled in plan["jobs"].items() if enabled]
    sys.stdout.write(f"Changed paths: {len(plan['changed_paths'])}\n")
    sys.stdout.write(f"Selected jobs: {', '.join(selected) if selected else 'none'}\n")
    for job in selected:
        for reason in plan["job_reasons"][job]:
            sys.stdout.write(f"  {job}: {reason}\n")
    if plan["native_targets"]:
        sys.stdout.write(f"Native targets: {', '.join(plan['native_targets'])}\n")
    if plan["pnpm_packages"]:
        sys.stdout.write(f"pnpm packages: {', '.join(plan['pnpm_packages'])}\n")


def _write_github_outputs(path: Path, plan: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for job, selected in plan["jobs"].items():
            stream.write(f"{job}={str(selected).lower()}\n")
        for key in ("native_targets", "native_languages", "pnpm_packages"):
            stream.write(f"{key}={json.dumps(plan[key], separators=(',', ':'))}\n")


def _tracked_paths() -> list[str]:
    result = subprocess.run(  # noqa: S603
        [GIT, "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True, timeout=30
    )
    return result.stdout.decode("utf-8", errors="surrogateescape").split("\0")[:-1]


def main(argv: list[str] | None = None) -> int:
    """Run a local inspection command or the CI plan command."""
    args = _parser().parse_args(argv)
    try:
        if args.command == "run-native":
            run_native_targets(args.targets_json)
            return 0
        components, ignored_roots, ignored_files = load_policy()
        if args.command == "validate":
            select(_tracked_paths(), components, ignored_roots, ignored_files)
            sys.stdout.write(
                f"Valid graph: {len(components)} components; all tracked paths classified.\n"
            )
            return 0
        paths = (
            [args.path]
            if args.command == "explain"
            else changed_paths(args.base, args.head, args.event)
        )
        plan = select(paths, components, ignored_roots, ignored_files)
        _display(plan, as_json=args.json)
        if args.command == "plan" and args.github_output:
            _write_github_outputs(args.github_output, plan)
    except (
        OSError,
        SelectionError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        tomllib.TOMLDecodeError,
    ) as error:
        sys.stderr.write(f"CI impact failed: {error}\n")
        return 2
    return 0
