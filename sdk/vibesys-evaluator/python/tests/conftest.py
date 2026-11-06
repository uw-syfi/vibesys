"""The core binary the wrappers call: ``$VIBESYS_EVALUATOR_BIN``, one on ``PATH``, else built with go."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vibesys_evaluator._private import core

GO_MODULE = Path(__file__).resolve().parents[2] / "go"
BUILD_TIMEOUT_SECS = 600


@pytest.fixture(scope="session")
def core_binary(tmp_path_factory: pytest.TempPathFactory) -> str | None:
    """The core binary to run the wrappers against; None when none can be found or built."""
    named = os.environ.get(core.BIN_ENV)
    if named:
        return named
    found = shutil.which(core.BIN_NAME)
    if found:
        return found
    go = shutil.which("go")
    if go is None:
        return None
    out = tmp_path_factory.mktemp("core-bin") / core.BIN_NAME
    # lint-waiver: LW-847201 [S603]; argv is the go found on PATH building this repository's own module.
    built = subprocess.run(  # noqa: S603
        [go, "build", "-o", str(out), f"./cmd/{core.BIN_NAME}"],
        cwd=GO_MODULE,
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT_SECS,
        check=False,
    )
    if built.returncode != 0:
        pytest.fail(f"building {core.BIN_NAME} failed:\n{built.stderr}", pytrace=False)
    return str(out)


@pytest.fixture(autouse=True)
def _core_env(core_binary: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point every test at the core binary (a test may still replace it)."""
    if core_binary is not None:
        monkeypatch.setenv(core.BIN_ENV, core_binary)
