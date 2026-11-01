"""Execute the type-check entry point with a recording uv instead of the type checker."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vs_sim.api import DISCARD, OsProcesses, ProcessOutcome, ProcessSpec
from vs_sim.api.testing import HANG_GUARD_S

ROOT = Path(__file__).resolve().parents[2]
# Each call records into a file of its own: the script runs its two checks side by side, and two
# calls appending to one file would interleave their lines.
_RECORD = 'printf "%s\\n" "$@" > "$UV_ARGUMENTS.$$"\n'


def _run_script(
    tmp_path: Path, after_recording: str = "", *, lagging: str | None = None
) -> tuple[ProcessOutcome, Path]:
    """Run the script with a fake uv that records each call, then runs *after_recording*.

    Which of the two side-by-side checks reaches its record first is up to the machine, so a test
    reads the calls by platform. *lagging* names a platform whose call waits before it records, to
    make the other one first on purpose.
    """
    # A real shell is necessary to observe quoting, exec, and PATH resolution.
    fake_uv = tmp_path / "uv"
    record = tmp_path / "arguments"
    lag = "" if lagging is None else f'case "$*" in *{lagging}*) sleep 0.3 ;; esac\n'
    fake_uv.write_text(f"#!/bin/sh\n{lag}{_RECORD}{after_recording}", encoding="utf-8")
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


@pytest.mark.parametrize("lagging", [None, "linux", "darwin"])
def test_the_typecheck_script_checks_linux_and_macos(tmp_path: Path, lagging: str | None) -> None:
    result, record = _run_script(tmp_path, lagging=lagging)

    assert result.returncode == 0, result.stderr
    invocations = [
        path.read_text(encoding="utf-8").split()
        for path in sorted(tmp_path.glob(f"{record.name}.*"))
    ]
    assert [item[:3] for item in invocations] == [["run", "ty", "check"]] * 2, invocations
    by_platform = {item[item.index("--python-platform") + 1]: item for item in invocations}
    assert sorted(by_platform) == ["darwin", "linux"], invocations
    darwin, linux = by_platform["darwin"], by_platform["linux"]
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
