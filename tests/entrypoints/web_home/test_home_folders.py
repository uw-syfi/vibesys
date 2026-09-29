from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

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


@pytest.mark.parametrize("path", ["relative/dir", "/", "/etc"])
def test_paths_outside_the_roots_or_relative_are_rejected(home: Home, path: str) -> None:
    code = home.get(f"/api/fs?path={path}").json()["error"]["code"]

    assert code in {"invalid_path", "outside_roots"}


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
