"""Execute the type-check entry point with a recording uv instead of the type checker."""

from __future__ import annotations

import os
from pathlib import Path

from vs_sim.api import DISCARD, OsProcesses, ProcessOutcome, ProcessSpec
from vs_sim.api.testing import HANG_GUARD_S

ROOT = Path(__file__).resolve().parents[2]
_END = "--end-of-invocation--"
_RECORD = f'printf "%s\\n" "$@" >> "$UV_ARGUMENTS"\nprintf "%s\\n" {_END} >> "$UV_ARGUMENTS"\n'


def _run_script(tmp_path: Path, after_recording: str = "") -> tuple[ProcessOutcome, Path]:
    """Run the script with a fake uv that records each call, then runs *after_recording*."""
    # A real shell is necessary to observe quoting, exec, and PATH resolution.
    fake_uv = tmp_path / "uv"
    record = tmp_path / "arguments"
    fake_uv.write_text(f"#!/bin/sh\n{_RECORD}{after_recording}", encoding="utf-8")
    fake_uv.chmod(0o755)
    result = OsProcesses().run(
        ProcessSpec(
            argv=(str(ROOT / "scripts" / "check_types.sh"),),
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "UV_ARGUMENTS": str(record),
            },
        ),
        timeout=HANG_GUARD_S,
        output=DISCARD,
    )
    assert isinstance(result, ProcessOutcome), result
    return result, record


def test_the_typecheck_script_checks_linux_and_macos(tmp_path: Path) -> None:
    result, record = _run_script(tmp_path)

    assert result.returncode == 0, result.stderr
    invocations = [
        block.split() for block in record.read_text(encoding="utf-8").split(_END) if block.strip()
    ]
    assert [item[:3] for item in invocations] == [["run", "ty", "check"]] * 2, invocations
    platforms = [item[item.index("--python-platform") + 1] for item in invocations]
    assert platforms == ["linux", "darwin"], invocations
    linux, darwin = invocations
    expected = {
        "src",
        *(str(path.relative_to(ROOT)) for path in ROOT.glob("libs/*/src")),
        *(str(path.relative_to(ROOT)) for path in ROOT.glob("sdk/*/src")),
    }
    assert set(darwin[darwin.index("darwin") + 1 :]) == expected, darwin
    assert linux == ["run", "ty", "check", "--python-platform", "linux"], linux


def test_an_api_macos_lacks_fails_the_script_and_the_message_names_the_fix(tmp_path: Path) -> None:
    result, _ = _run_script(tmp_path, 'case "$*" in *darwin*) exit 1 ;; esac\n')

    assert result.returncode != 0
    assert b"capability probe" in b" ".join(result.stderr.split()), result.stderr
