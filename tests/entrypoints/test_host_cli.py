"""Host maintenance dispatch uses the existing console launcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.host import run
from entrypoints.launcher import main

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_convert_missing_run_reports_why_it_was_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state-home"))
    root = tmp_path / "project"
    root.mkdir()
    assert run(["convert-run", "run-1", "--project", str(root)], cwd=tmp_path) == (
        0,
        "skipped: journal is missing\n",
        "",
    )


def test_launcher_dispatches_host_and_reports_an_invalid_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "state-home"))
    assert main(["host", "convert-run", "../escape", "--project", str(tmp_path)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "vibesys host convert-run" in captured.err
    assert "../escape" in captured.err
