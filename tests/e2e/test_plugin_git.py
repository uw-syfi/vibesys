"""Standalone plugin Git boundary against real git, confined to the real tier."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, _ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_host_git_obeys_controller_contract(tmp_path: Path) -> None:
    module = _load("plugin_git_contract", "skills/search/optimize/tests/test_run.py")
    git = module.controller.HostGit()
    git(tmp_path, ["init"])
    for message in ("base", "candidate"):
        git(
            tmp_path,
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                message,
            ],
        )
        if message == "base":
            base = git(tmp_path, ["rev-parse", "HEAD"]).strip()
    candidate = git(tmp_path, ["rev-parse", "HEAD"]).strip()
    git(tmp_path, ["reset", "--hard", base])
    module.exercise_git_contract(git, tmp_path, base, candidate)


def test_controller_transaction_excludes_another_process(tmp_path: Path) -> None:
    module = _load("plugin_lock_contract", "skills/search/optimize/tests/test_run.py")
    run = module.Run(tmp_path)

    class InspectingGit(module.Git):
        def alive(self, _pid: int) -> bool:
            program = """import fcntl, sys
with open(sys.argv[1], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
"""
            # LW-100360 [S603]; fixed Python probe argv, no shell; hardcoding a Python
            # path breaks virtualenvs, and a shell adds quoting hazards.
            result = subprocess.run(  # noqa: S603
                [sys.executable, "-c", program, str(run.path / "run.lock")],
                timeout=30,
                check=False,
            )
            assert result.returncode == 0
            return False

    run.git = InspectingGit(tmp_path)
    assert run.call("track", "--id", "x", "--description", "work", "--pid", "123") == 0
    assert run.call("untrack", "--id", "x") == 0


def test_service_owner_stops_and_verifies_its_process_group() -> None:
    module = _load("plugin_service_contract", "skills/search/measure/scripts/service.py")
    assert (
        module.serve(
            [sys.executable, "-c", "import signal; signal.pause()"],
            [sys.executable, "-c", "pass"],
            [sys.executable, "-c", "pass"],
            module.PosixHost(),
        )
        == 0
    )


def test_benchmark_wrapper_holds_the_host_lock_in_its_child() -> None:
    module = _load("benchmark_lock_contract", "skills/search/measure/scripts/measurements.py")
    program = """import fcntl, sys
with open("/tmp/vibesys-benchmark.lock", "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(0)
    sys.exit(1)
"""
    assert (
        module.main(
            ["run", "--", sys.executable, "-c", program],
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
            out=io.StringIO(),
            err=io.StringIO(),
            host="host-a",
        )
        == 0
    )
