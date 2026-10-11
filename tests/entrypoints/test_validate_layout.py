"""``vibesys validate`` accepts only the task layout and names the fix for a root manifest."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.task_project import write_task

from entrypoints.cli import _run_validate
from vibesys.api import ConfigurationError

if TYPE_CHECKING:
    from pathlib import Path

_MANIFEST = (
    'version = 1\n[agent]\ndomain = "generic"\n'
    '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
)


@pytest.mark.parametrize("task", ["default", "queue", "a-b-1"])
def test_a_root_manifest_is_a_configuration_error_naming_the_task_location(
    tmp_path: Path, task: str
) -> None:
    write_task(tmp_path, _MANIFEST, task=task)
    (tmp_path / "vibesys.input.toml").write_text(_MANIFEST, encoding="utf-8")

    with pytest.raises(ConfigurationError) as raised:
        _run_validate([str(tmp_path)])

    message = raised.value.diagnostic.message
    assert "vibesys.input.toml" in message
    assert ".vibesys/tasks/<task>/vibesys.input.toml" in message
    assert raised.value.diagnostic.code == "validation_failed"


def test_a_root_manifest_without_any_task_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "vibesys.input.toml").write_text(_MANIFEST, encoding="utf-8")
    (tmp_path / "OBJECTIVE.md").write_text("Optimize.\n", encoding="utf-8")

    with pytest.raises(ConfigurationError) as raised:
        _run_validate([str(tmp_path)])

    assert ".vibesys/tasks/<task>/vibesys.input.toml" in raised.value.diagnostic.message


def test_a_task_layout_project_validates(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_task(tmp_path, _MANIFEST, task="queue")

    _run_validate([str(tmp_path), "--task", "queue"])

    assert "VibeSys validation passed" in capsys.readouterr().out
