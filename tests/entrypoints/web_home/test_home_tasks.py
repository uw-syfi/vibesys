"""Tests for task list and detail (`entrypoints.web_home.tasks`)."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from tests.entrypoints.web_home.support import make_project, project_key

if TYPE_CHECKING:
    from pathlib import Path

    from tests.entrypoints.web_home.support import Home


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
