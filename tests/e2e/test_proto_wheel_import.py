"""Import and validate the real built wheel in an isolated Python process."""

from __future__ import annotations

import sys
import sysconfig
import zipfile
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

if TYPE_CHECKING:
    from tests.support.distribution_build import BuiltWheel


@pytest.mark.xdist_group("built_wheel")
def test_built_wheel_imports_and_validates_proto_contracts(built_wheel: BuiltWheel) -> None:
    """Exercise the shipped package without editable source paths or site startup."""
    source = built_wheel.source.root / "libs/vs-proto/src"
    with zipfile.ZipFile(built_wheel.wheel) as archive:
        for module in (source / "vs_proto").rglob("*.py"):
            member = module.relative_to(source).as_posix()
            assert archive.read(member) == module.read_bytes(), member
    script = """
import sys
sys.path[:0] = sys.argv[1:3]
from vs_proto.api import ValidationError, replay, validate
validate(replay.ReplayPolicy(reply_type="test.Reply"))
try:
    validate(replay.ReplayPolicy())
except ValidationError:
    pass
else:
    raise AssertionError("installed validation accepted an invalid reply contract")
"""
    result = run_test_command(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            script,
            str(built_wheel.wheel),
            sysconfig.get_path("purelib"),
        ],
        cwd=built_wheel.wheel.parent,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout
