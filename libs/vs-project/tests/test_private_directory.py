"""The rule for a directory only its owner may use, and where a user's runtime root is."""

from __future__ import annotations

import os
import stat
from itertools import count
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import PrivateDirectoryError, ensure_private_directory, user_runtime_root

if TYPE_CHECKING:
    from pathlib import Path

_UID = os.getuid()
_PERMISSION_BITS = st.integers(min_value=0o000, max_value=0o777)
_FRESH = count()


def _fresh(tmp_path: Path) -> Path:
    return tmp_path / f"case-{next(_FRESH)}"


@pytest.mark.parametrize("environ", [{}, {"XDG_RUNTIME_DIR": ""}, {"XDG_RUNTIME_DIR": "run/user"}])
def test_without_an_absolute_runtime_dir_the_root_is_a_fixed_per_user_path(
    environ: dict[str, str],
) -> None:
    assert str(user_runtime_root(environ, 1001)) == "/tmp/vibesys-1001"  # noqa: S108  # lint-waiver: LW-178204 [S108]; the rule fixes this literal path.


@given(
    st.from_regex(r"/[a-z0-9]{1,12}(/[a-z0-9]{1,12}){0,3}", fullmatch=True), st.integers(0, 2**31)
)
def test_an_absolute_runtime_dir_holds_the_root_for_any_uid(runtime: str, uid: int) -> None:
    assert str(user_runtime_root({"XDG_RUNTIME_DIR": runtime}, uid)) == f"{runtime}/vibesys"


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mode=_PERMISSION_BITS)
def test_an_own_directory_is_used_and_left_private_unless_others_can_write(
    tmp_path: Path, mode: int
) -> None:
    directory = _fresh(tmp_path)
    directory.mkdir()
    directory.chmod(mode)
    try:
        if mode & 0o022:
            with pytest.raises(PrivateDirectoryError):
                ensure_private_directory(directory, _UID)
            assert stat.S_IMODE(directory.stat().st_mode) == mode
        else:
            assert ensure_private_directory(directory, _UID) == directory
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    finally:
        directory.chmod(0o700)


def test_a_missing_directory_is_created_private(tmp_path: Path) -> None:
    directory = tmp_path / "new"

    assert ensure_private_directory(directory, _UID) == directory
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


def test_a_directory_of_another_owner_is_refused_and_left_alone(tmp_path: Path) -> None:
    directory = tmp_path / "theirs"
    directory.mkdir(mode=0o755)
    directory.chmod(0o755)

    with pytest.raises(PrivateDirectoryError, match=f"owned by uid {_UID + 1}"):
        ensure_private_directory(directory, _UID + 1)

    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_a_symlink_or_file_in_the_way_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")

    with pytest.raises(PrivateDirectoryError):
        ensure_private_directory(link, _UID)
    with pytest.raises(FileExistsError):
        ensure_private_directory(blocker, _UID)
