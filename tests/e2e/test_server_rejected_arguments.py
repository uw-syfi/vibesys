"""Rejected server launches release process-local ephemeral socket resources."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["--web-port", "99999"], id="invalid-web-port"),
        pytest.param(
            ["--web-origin", "http://localhost:5173/app"],
            id="invalid-web-origin",
        ),
        pytest.param(["--web-origin"], id="missing-web-origin"),
    ],
)
def test_a_rejected_web_flag_creates_no_ephemeral_socket_directory(
    arguments: list[str],
    tmp_path: Path,
) -> None:
    invocation = ["--web", "--web-instance", str(tmp_path / "instance.json"), *arguments]

    assert _rejected_launch_probe(invocation, tmp_path) == {"exit_code": 2, "entries": []}


def test_a_missing_control_socket_creates_no_ephemeral_web_socket_directory(
    tmp_path: Path,
) -> None:
    assert _rejected_launch_probe([], tmp_path) == {"exit_code": 2, "entries": []}


def _rejected_launch_probe(arguments: list[str], tmp_path: Path) -> object:
    """Call public main and inspect its temp root while the failure remains live."""
    temp_root = tmp_path / "temp"
    temp_root.mkdir()
    probe = """
import json
import sys
from pathlib import Path

from entrypoints.server import main

failure = None
try:
    main(json.loads(sys.argv[1]))
except SystemExit as error:
    failure = error
if failure is None:
    raise AssertionError("server entrypoint accepted rejected arguments")
print(json.dumps({"exit_code": failure.code, "entries": sorted(path.name for path in Path(sys.argv[2]).iterdir())}))
"""
    completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-101110 [S603]; execute the current interpreter with a fixed inline probe and fixed arguments in the real-API e2e tier
        [sys.executable, "-c", probe, json.dumps(arguments), str(temp_root)],
        env={**os.environ, "TMPDIR": str(temp_root)},
        capture_output=True,
        text=True,
        check=True,
    )

    return json.loads(completed.stdout)
