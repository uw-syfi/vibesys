"""Tests for task list and detail (`entrypoints.web_home.tasks`)."""

from __future__ import annotations

import shutil
import tomllib
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.web_home.support import make_project, project_key
from tests.support import run_test_command

if TYPE_CHECKING:
    from pathlib import Path

    from tests.entrypoints.web_home.support import Home

FORM = {
    "objective": "Raise throughput.\n",
    "domain": "generic",
    "accuracy_command": "python check.py --strict",
    "benchmark_command": "python 'bench suite.py'",
    "result_json_argument": "--json",
    "result_metric": "tokens_per_s",
}


def _setup(
    home: Home, *, tasks: tuple[str, ...] = ("bench",), commit: bool = True
) -> tuple[str, Path]:
    root = make_project(home.workspace / "proj", tasks=tasks, commit=commit)
    return project_key(home, root), root


def test_tasks_list_and_detail_expose_the_manifest(home: Home) -> None:
    key, _ = _setup(home)

    listing = home.get(f"/api/projects/{key}/tasks").json()
    detail = home.get(f"/api/projects/{key}/tasks/bench").json()

    assert listing == {
        "tasks": [{"name": "bench", "valid": True, "domain": "generic", "error": None}]
    }
    assert detail["objective"] == "Make bench faster.\n"
    assert detail["benchmark_command"] == "python bench.py"
    assert detail["result"] == {
        "kind": "metric",
        "json_argument": "--json",
        "metric": "throughput",
        "protocol_version": None,
    }
    assert (detail["editable"], detail["read_only_reason"]) == (True, None)
    assert len(detail["content_hash"]) == 64


def test_unknown_project_and_task_are_typed_errors(home: Home) -> None:
    key, _ = _setup(home)

    assert home.get("/api/projects/0000/tasks").json()["error"]["code"] == "unknown_project"
    assert home.get(f"/api/projects/{key}/tasks/nope").json()["error"]["code"] == "unknown_task"


def test_removed_project_root_is_a_typed_error(home: Home) -> None:
    key, root = _setup(home)
    shutil.rmtree(root)

    reply = home.get(f"/api/projects/{key}/tasks")

    assert reply.status < 500
    assert reply.json()["error"]["code"] == "unknown_project"


def test_malformed_task_file_is_a_typed_error(home: Home) -> None:
    key, root = _setup(home)
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    manifest.write_text("this is not [ valid toml")

    reply = home.get(f"/api/projects/{key}/tasks/bench")

    assert reply.status < 500
    assert reply.json()["error"]["code"] == "task_invalid"


def _make_protocol_task(root: Path) -> None:
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    text = manifest.read_text().split("[benchmark.result]")[0]
    manifest.write_text(
        text.replace(
            'command = ["python", "bench.py"]',
            'command = ["python", "bench.py"]\nresult_protocol = 2',
        )
    )


def test_protocol_tasks_are_read_only(home: Home) -> None:
    key, root = _setup(home)
    _make_protocol_task(root)

    detail = home.get(f"/api/projects/{key}/tasks/bench").json()

    assert detail["result"] == {
        "kind": "protocol",
        "json_argument": None,
        "metric": None,
        "protocol_version": 2,
    }
    assert detail["editable"] is False
    assert "[benchmark.result]" in detail["read_only_reason"]


def test_create_writes_both_files_and_the_task_loads(home: Home) -> None:
    key, root = _setup(home, tasks=())

    created = home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"}).json()

    assert created["benchmark_command"] == "python 'bench suite.py'"
    assert (root / ".vibesys" / "tasks" / "serve" / "OBJECTIVE.md").read_text() == FORM["objective"]
    manifest = tomllib.loads(
        (root / ".vibesys" / "tasks" / "serve" / "vibesys.input.toml").read_text()
    )
    assert manifest["benchmark"]["result"] == {"json_argument": "--json", "metric": "tokens_per_s"}
    again = home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"}).json()
    assert again["error"]["code"] == "task_exists"


def test_create_with_a_del_character_round_trips(home: Home) -> None:
    key, _ = _setup(home, tasks=())

    created = home.post(
        f"/api/projects/{key}/tasks", {**FORM, "name": "serve", "result_metric": "a\x7fb"}
    ).json()

    assert created["result"]["metric"] == "a\x7fb"
    loaded = home.get(f"/api/projects/{key}/tasks/serve").json()
    assert loaded["result"]["metric"] == "a\x7fb"


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"name": "Bad Name"}, "task_invalid"),
        ({"name": "ok", "benchmark_command": "   "}, "task_invalid"),
        ({"name": "ok", "accuracy_command": "python 'unterminated"}, "task_invalid"),
        ({"name": "ok", "result_json_argument": "json"}, "task_invalid"),
        ({"name": "ok", "domain": "astrology"}, "invalid_request"),
    ],
)
def test_create_rejects_invalid_forms(home: Home, change: dict[str, str], code: str) -> None:
    key, _ = _setup(home, tasks=())

    reply = home.post(f"/api/projects/{key}/tasks", {**FORM, **change})

    assert reply.json()["error"]["code"] == code


def test_edit_needs_the_current_hash(home: Home) -> None:
    key, root = _setup(home)
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    edited = home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base}).json()
    stale = home.put(
        f"/api/projects/{key}/tasks/bench",
        {**FORM, "objective": "Never applied.\n", "result_metric": "never_applied", "base_hash": base},
    ).json()

    assert edited["objective"] == FORM["objective"]
    assert edited["content_hash"] != base
    assert stale["error"]["code"] == "task_conflict"
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    assert (root / ".vibesys" / "tasks" / "bench" / "OBJECTIVE.md").read_text() == FORM["objective"]
    assert tomllib.loads(manifest.read_text())["benchmark"]["result"]["metric"] == FORM["result_metric"]


def test_edit_keeps_manifest_settings_the_form_does_not_show(home: Home) -> None:
    key, root = _setup(home)
    manifest = root / ".vibesys" / "tasks" / "bench" / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text().replace(
            'command = ["python", "bench.py"]',
            'command = ["python", "bench.py"]\ntimeout_seconds = 90',
        )
    )
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base})

    assert tomllib.loads(manifest.read_text())["benchmark"]["timeout_seconds"] == 90


def test_editing_a_read_only_task_is_refused(home: Home) -> None:
    key, root = _setup(home)
    _make_protocol_task(root)
    base = home.get(f"/api/projects/{key}/tasks/bench").json()["content_hash"]

    reply = home.put(f"/api/projects/{key}/tasks/bench", {**FORM, "base_hash": base})

    assert reply.json()["error"]["code"] == "task_read_only"


def test_commit_previews_then_commits_only_task_files(home: Home) -> None:
    key, root = _setup(home, tasks=())
    home.post(f"/api/projects/{key}/tasks", {**FORM, "name": "serve"})
    (root / "notes.txt").write_text("mine")
    (root / ".vibesys" / "web-gateway.json").write_text('{"token": "secret"}')

    preview = home.get(f"/api/projects/{key}/commit").json()
    stale = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"][:1]}).json()
    result = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"]}).json()

    assert preview == {
        "task_files": [
            ".vibesys/tasks/serve/OBJECTIVE.md",
            ".vibesys/tasks/serve/vibesys.input.toml",
        ],
        "other": [".vibesys/web-gateway.json", "notes.txt"],
    }
    assert stale["error"]["code"] == "task_conflict"
    assert result["committed"] == preview["task_files"]
    status = run_test_command(
        ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=True
    )
    assert status.stdout == "?? .vibesys/web-gateway.json\n?? notes.txt\n"


def test_commit_works_in_a_repository_without_commits(home: Home) -> None:
    key, root = _setup(home, commit=False)
    (root / "README.md").unlink()

    preview = home.get(f"/api/projects/{key}/commit").json()
    result = home.post(f"/api/projects/{key}/commit", {"paths": preview["task_files"]})

    assert result.status == 200
    assert home.post("/api/projects/validate", {"path": str(root)}).json()["state"] == "ready"
