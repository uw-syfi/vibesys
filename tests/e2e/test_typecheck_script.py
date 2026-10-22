"""Execute the type-check entry point with a recording uv instead of the type checker."""

from __future__ import annotations

import os
from pathlib import Path

from vs_sim.api import DISCARD, OsProcesses, ProcessOutcome, ProcessSpec
from vs_sim.api.testing import HANG_GUARD_S

ROOT = Path(__file__).resolve().parents[2]


def test_the_typecheck_script_pins_the_linux_platform(tmp_path: Path) -> None:
    # A real shell is necessary to observe quoting, exec, and PATH resolution.
    fake_uv = tmp_path / "uv"
    record = tmp_path / "arguments"
    fake_uv.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$UV_ARGUMENTS"\n', encoding="utf-8")
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
    assert result.returncode == 0, result.stderr
    arguments = record.read_text(encoding="utf-8").splitlines()
    assert arguments[:3] == ["run", "ty", "check"]
    assert any(
        arguments[index : index + 2] == ["--python-platform", "linux"]
        for index in range(len(arguments) - 1)
    ), arguments
