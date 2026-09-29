from __future__ import annotations

from typing import TYPE_CHECKING

from tests.entrypoints.web_home.support import make_project
from tests.support import run_test_command

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def _state(home: Home, path: object) -> str:
    return home.post("/api/projects/validate", {"path": str(path)}).json()["state"]


def test_validation_reports_the_first_blocker_in_order(home: Home) -> None:
    root = home.workspace / "p"
    assert _state(home, root) == "missing"
    root.mkdir()
    assert _state(home, root) == "not_git"
    run_test_command(["git", "init", "-q"], cwd=root, check=True)
    assert _state(home, root) == "uninitialized"
    (root / ".vibesys" / "tasks").mkdir(parents=True)
    assert _state(home, root) == "no_tasks"


def test_validation_distinguishes_commitless_dirty_and_ready(home: Home) -> None:
    assert _state(home, make_project(home.workspace / "fresh", commit=False)) == "no_commits"
    ready = make_project(home.workspace / "ready")
    assert _state(home, ready) == "ready"
    (ready / "scratch.txt").write_text("x")

    reply = home.post("/api/projects/validate", {"path": str(ready)}).json()

    assert (reply["state"], reply["pending"], reply["tasks"]) == (
        "dirty_tree",
        ["scratch.txt"],
        ["bench"],
    )


def test_a_symlinked_config_root_is_invalid(home: Home) -> None:
    root = make_project(home.workspace / "linked", tasks=())
    target = home.workspace / "elsewhere"
    target.mkdir()
    (root / ".vibesys").symlink_to(target)

    reply = home.post("/api/projects/validate", {"path": str(root)}).json()

    assert reply["state"] == "invalid"
    assert "symlink" in reply["message"]


def test_validated_git_work_trees_become_recent_projects(home: Home) -> None:
    first = make_project(home.workspace / "first")
    second = make_project(home.workspace / "second")
    (home.workspace / "plain").mkdir()
    for path in (first, second, home.workspace / "plain", first):
        home.post("/api/projects/validate", {"path": str(path)})

    projects = home.get("/api/projects").json()["projects"]

    assert [p["root"] for p in projects] == [str(first), str(second)]
    assert projects[0]["last_opened"] == "2026-09-28T12:00:00+00:00"


def test_validate_rejects_paths_outside_the_roots(home: Home) -> None:
    reply = home.post("/api/projects/validate", {"path": "/"})

    assert (reply.status, reply.json()["error"]["code"]) == (403, "outside_roots")
