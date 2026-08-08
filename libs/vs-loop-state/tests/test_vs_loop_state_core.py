"""Tests for the generic atomic JSON persistence primitives."""

from pathlib import Path

from vs_loop_state.core import atomic_write_json, read_json


def test_read_json_missing_path_returns_none(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") is None


def test_atomic_write_json_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1, "b": [1, 2, 3]})

    assert read_json(path) == {"a": 1, "b": [1, 2, 3]}


def test_atomic_write_json_creates_parent_directories(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "state.json"
    atomic_write_json(path, {"a": 1})

    assert read_json(path) == {"a": 1}


def test_atomic_write_json_does_not_leave_tmp_file_behind(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1})

    assert not (tmp_path / "state.json.tmp").exists()


def test_atomic_write_json_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    atomic_write_json(path, {"a": 1})
    atomic_write_json(path, {"a": 2})

    assert read_json(path) == {"a": 2}
