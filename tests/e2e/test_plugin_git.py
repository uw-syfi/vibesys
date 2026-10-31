"""Standalone plugin Git boundary against real git, confined to the real tier."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


def test_host_git_obeys_controller_contract(tmp_path: Path) -> None:
    source = Path(__file__).resolve().parents[2] / "skills/search/optimize/tests/test_run.py"
    spec = importlib.util.spec_from_file_location("plugin_git_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    source = Path(__file__).resolve().parents[2] / "skills/search/optimize/tests/test_run.py"
    spec = importlib.util.spec_from_file_location("plugin_lock_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
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
    source = Path(__file__).resolve().parents[2] / "skills/search/measure/scripts/service.py"
    spec = importlib.util.spec_from_file_location("plugin_service_contract", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (
        module.serve(
            [sys.executable, "-c", "import signal; signal.pause()"],
            [sys.executable, "-c", "pass"],
            [sys.executable, "-c", "pass"],
            module.PosixHost(),
        )
        == 0
    )
