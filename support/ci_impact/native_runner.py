"""Run selected Cargo and Go checks for registered native CI roots."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from pathlib import Path

from .model import ROOT, _fail, _safe_path

SDK_ROOT = "sdk/vs-evaluator/vseval"
SDK_MODULE = "github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval"
MICROSERVICE_ROOT = "resources/evaluators/microservice"
CommandRunner = Callable[[list[str], Path, dict[str, str]], None]


def _default_runner(command: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(  # noqa: S603
        command, cwd=cwd, env=env, check=True, timeout=600
    )


def _registered_roots(root: Path) -> tuple[str, ...]:
    with (root / "ci-components.toml").open("rb") as stream:
        value = tomllib.load(stream).get("native_ci_roots")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        _fail("ci-components.toml: native_ci_roots must be a list of paths")
    return tuple(_safe_path(item, "native_ci_roots") for item in value)


def _selected_roots(raw_targets: str, registered: tuple[str, ...]) -> list[str]:
    try:
        selected = json.loads(raw_targets)
    except json.JSONDecodeError as error:
        _fail(f"native targets must be a JSON array: {error}")
    if (
        not isinstance(selected, list)
        or not selected
        or not all(isinstance(item, str) for item in selected)
    ):
        _fail("native targets must be a nonempty JSON array of paths")
    if len(selected) != len(set(selected)):
        _fail("native targets contain duplicate paths")
    unknown = sorted(set(selected) - set(registered))
    if unknown:
        _fail(
            f"unregistered native targets: {', '.join(unknown)}; "
            "add each target to ci-components.toml native_ci_roots"
        )
    return selected


def _check_sdk_module(root: Path) -> None:
    go_mod = root / SDK_ROOT / "go.mod"
    declared = next(
        (
            line.removeprefix("module ").strip()
            for line in go_mod.read_text(encoding="utf-8").splitlines()
            if line.startswith("module ")
        ),
        None,
    )
    if declared != SDK_MODULE:
        _fail(f"{go_mod}: declares module {declared!r}; expected {SDK_MODULE!r}")


def run_native_targets(
    raw_targets: str,
    *,
    root: Path = ROOT,
    runner: CommandRunner = _default_runner,
) -> None:
    """Validate selected roots and run their native checks in selection order."""
    selected = _selected_roots(raw_targets, _registered_roots(root))
    for target in selected:
        directory = root / target
        if target == SDK_ROOT:
            _check_sdk_module(root)
        env = os.environ.copy()
        if target == MICROSERVICE_ROOT:
            env["VIBESYS_REQUIRE_MANAGED_CANDIDATE_TESTS"] = "1"
        if (directory / "Cargo.toml").is_file():
            commands = [
                ["cargo", "fmt", "--", "--check"],
                ["cargo", "clippy", "--locked", "--all-targets", "--", "-D", "warnings"],
                ["cargo", "test", "--locked"],
            ]
        elif (directory / "go.mod").is_file():
            commands = [["go", "test", "-race", "./..."]]
        else:
            _fail(f"native target {target!r} has no Cargo.toml or go.mod")
        for command in commands:
            sys.stdout.write(f"{target}: {' '.join(command)}\n")
            sys.stdout.flush()
            runner(command, directory, env)
