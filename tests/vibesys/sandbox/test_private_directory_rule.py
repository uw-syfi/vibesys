"""The Modal evaluator helper applies the same private-directory rule as ``vs_project``.

The helper is mounted alone into the agent container and cannot import
``vs_project.api.ensure_private_directory`` or ``user_runtime_root``, so it restates them.
Two statements of one rule drift unless something runs both on the same input.
"""

from __future__ import annotations

import os
import stat
from itertools import count
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import PrivateDirectoryError, ensure_private_directory, user_runtime_root
from vs_sandbox import (
    modal_evaluator,  # test-isolation: the helper is mounted alone, so this suite compares its private runtime-directory functions with the owner's.
)

if TYPE_CHECKING:
    from pathlib import Path

_FRESH = count()
_SHAPES = ("absent", "directory", "symlink", "file")


@pytest.mark.parametrize(
    "xdg", [None, "", "relative/run", "/run/user/1000", "/a b/run", "/run/user/../1000"]
)
def test_both_place_the_runtime_directory_alike(xdg: str | None) -> None:
    with pytest.MonkeyPatch.context() as env:
        if xdg is None:
            env.delenv("XDG_RUNTIME_DIR", raising=False)
        else:
            env.setenv("XDG_RUNTIME_DIR", xdg)

        assert modal_evaluator._runtime_dir() == user_runtime_root(os.environ, os.getuid())  # noqa: SLF001  # lint-waiver: LW-178205 [SLF001]; the helper is mounted alone, so the test compares its private function with the owner's.


def _owner_accepts(directory: Path) -> bool:
    try:
        ensure_private_directory(directory, os.getuid())
    except (PrivateDirectoryError, OSError):
        return False
    return True


def _helper_accepts(directory: Path) -> bool:
    try:
        modal_evaluator._ensure_runtime_dir(directory / "lock")  # noqa: SLF001  # lint-waiver: LW-178206 [SLF001]; the helper is mounted alone, so the test compares its private function with the owner's.
    except (RuntimeError, OSError):
        return False
    return True


def _build(tmp_path: Path, shape: str, mode: int) -> Path:
    directory = tmp_path / f"case-{next(_FRESH)}"
    if shape == "directory":
        directory.mkdir()
        directory.chmod(mode)
    elif shape == "symlink":
        target = tmp_path / f"target-{next(_FRESH)}"
        target.mkdir(mode=0o700)
        directory.symlink_to(target, target_is_directory=True)
    elif shape == "file":
        directory.write_text("in the way")
    return directory


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(shape=st.sampled_from(_SHAPES), mode=st.integers(min_value=0o000, max_value=0o777))
def test_both_accept_and_refuse_the_same_directories(tmp_path: Path, shape: str, mode: int) -> None:
    owner_dir = _build(tmp_path, shape, mode)
    helper_dir = _build(tmp_path, shape, mode)
    try:
        owner_accepted = _owner_accepts(owner_dir)
        helper_accepted = _helper_accepts(helper_dir)

        assert helper_accepted == owner_accepted, (shape, oct(mode))
        if owner_accepted:
            for accepted in (owner_dir, helper_dir):
                assert stat.S_IMODE(accepted.stat().st_mode) == 0o700
    finally:
        for built in (owner_dir, helper_dir):
            if built.is_dir() and not built.is_symlink():
                built.chmod(0o700)
