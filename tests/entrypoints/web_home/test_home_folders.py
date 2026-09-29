from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from entrypoints.web_home import projects
from entrypoints.web_home.contract import ApiError, ErrorCode

if TYPE_CHECKING:
    from pathlib import Path

    from tests.entrypoints.web_home.support import Home


def test_without_a_path_the_listing_is_the_granted_roots(home: Home) -> None:
    listing = home.get("/api/fs").json()

    assert listing["path"] is None
    assert [entry["path"] for entry in listing["entries"]] == [str(home.workspace)]


def test_listing_shows_visible_subfolders_with_canonical_paths(home: Home) -> None:
    (home.workspace / "b-repo" / ".git").mkdir(parents=True)
    (home.workspace / "a-plain").mkdir()
    (home.workspace / ".hidden").mkdir()
    (home.workspace / "file.txt").write_text("x")

    listing = home.get(f"/api/fs?path={home.workspace}").json()

    assert [(e["name"], e["git"]) for e in listing["entries"]] == [
        ("a-plain", False),
        ("b-repo", True),
    ]
    assert listing["parent"] is None


def test_symlinks_that_leave_the_roots_are_hidden_and_refused(home: Home, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home.workspace / "escape").symlink_to(outside)
    (home.workspace / "inner").mkdir()
    (home.workspace / "alias").symlink_to(home.workspace / "inner")

    entries = home.get(f"/api/fs?path={home.workspace}").json()["entries"]

    assert [(e["name"], e["path"]) for e in entries] == [
        ("alias", str(home.workspace / "inner")),
        ("inner", str(home.workspace / "inner")),
    ]
    refused = home.get(f"/api/fs?path={home.workspace / 'escape'}").json()
    assert refused["error"]["code"] == "outside_roots"


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("relative/dir", "invalid_path"),
        ("/", "outside_roots"),
        ("/etc", "outside_roots"),
    ],
)
def test_paths_outside_the_roots_or_relative_are_rejected(home: Home, path: str, code: str) -> None:
    assert home.get(f"/api/fs?path={path}").json()["error"]["code"] == code


def test_a_sibling_folder_with_a_prefixed_name_is_outside_the_root(home: Home) -> None:
    sibling = home.workspace.with_name(home.workspace.name + "-sibling")

    with pytest.raises(ApiError) as excinfo:
        projects.confine(home.config, str(sibling))

    assert excinfo.value.code == ErrorCode.OUTSIDE_ROOTS


def test_a_dotdot_escape_above_the_root_is_rejected(home: Home) -> None:
    with pytest.raises(ApiError) as excinfo:
        projects.confine(home.config, str(home.workspace / ".."))

    assert excinfo.value.code == ErrorCode.OUTSIDE_ROOTS


def test_a_nul_byte_in_the_path_is_rejected(home: Home) -> None:
    with pytest.raises(ApiError) as excinfo:
        projects.confine(home.config, str(home.workspace / "evil") + "\0")

    assert excinfo.value.code == ErrorCode.INVALID_PATH


def test_a_symlink_loop_is_rejected_as_invalid_path_not_a_500(home: Home) -> None:
    loop = home.workspace / "loop"
    loop.symlink_to(loop)

    reply = home.get(f"/api/fs?path={loop}").json()

    assert reply["error"]["code"] == "invalid_path"


def test_a_symlink_loop_inside_a_listed_folder_does_not_break_the_listing(home: Home) -> None:
    (home.workspace / "loop").symlink_to(home.workspace / "loop")
    (home.workspace / "plain").mkdir()

    reply = home.get(f"/api/fs?path={home.workspace}").json()

    assert [entry["name"] for entry in reply["entries"]] == ["plain"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_an_unreadable_folder_reports_permission_denied(home: Home) -> None:
    locked = home.workspace / "locked"
    locked.mkdir()
    locked.chmod(0)
    try:
        reply = home.get(f"/api/fs?path={locked}").json()
    finally:
        locked.chmod(0o755)

    assert reply["error"]["code"] == "permission_denied"
