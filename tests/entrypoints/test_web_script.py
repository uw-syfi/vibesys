"""Regression tests for the browser-only source-checkout launcher."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _recording_executable(path: Path, marker_name: str) -> None:
    path.write_text(f'#!/usr/bin/env bash\nprintf \'%s\\n\' "$PWD" "$@" > "${{{marker_name}}}"\n')
    path.chmod(0o755)


def test_web_ui_script_bypasses_tui_launcher_and_bootstraps_clients(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    fake_bin = tmp_path / "bin"
    root.mkdir()
    fake_bin.mkdir()
    script = Path(__file__).resolve().parents[2] / "scripts" / "run-web-ui.sh"
    pnpm_marker = tmp_path / "pnpm.txt"
    uv_marker = tmp_path / "uv.txt"
    _recording_executable(fake_bin / "pnpm", "PNPM_MARKER")
    _recording_executable(fake_bin / "uv", "UV_MARKER")
    environment = {
        **os.environ,
        "PATH": os.pathsep.join((str(fake_bin), os.defpath)),
        "PNPM_MARKER": str(pnpm_marker),
        "UV_MARKER": str(uv_marker),
    }

    # test-isolation: execute only the repository script with fake uv and pnpm binaries.
    subprocess.run(  # noqa: S603  # lint-waiver: LW-101102 [S603]; execute the fixed repository launcher against test-owned fake tools
        [str(script), "--port", "9123"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    repository_root = str(script.parents[1])
    assert pnpm_marker.read_text().splitlines() == [
        repository_root,
        "--dir",
        "clients",
        "install",
        "--frozen-lockfile",
    ]
    assert uv_marker.read_text().splitlines() == [
        repository_root,
        "run",
        "python",
        "-m",
        "entrypoints.web",
        "live",
        "--demo",
        "--open",
        "--port",
        "9123",
    ]
