"""Tests for the ``vibesys`` console entry point."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from entrypoints import launcher as cli
from vs_sim.api.testing import FakeForegroundLauncher, FakeSignalSource, ForegroundScript

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize(
    ("args", "module"),
    [
        (["--input", "example.py"], "entrypoints.headless"),
        (["validate", "bundle"], "entrypoints.headless"),
        (["--detach", "--input", "example.py"], "entrypoints.server"),
        (["--web", "--input", "example.py"], "entrypoints.server"),
    ],
)
def test_launcher_routes_run_commands_to_python(args: list[str], module: str) -> None:
    assert cli.headless_command(args) == [sys.executable, "-m", module, *args]


@pytest.mark.parametrize("status", [0, 3, -9])
def test_run_child_status_is_the_launchers(status: int) -> None:
    children = FakeForegroundLauncher(
        lambda _argv: ForegroundScript(returncode=status, exits_immediately=True)
    )
    host = cli.LauncherHost(children=children, signals=FakeSignalSource())

    result = cli.main(["--input", "example.py"], host)

    assert result == (128 - status if status < 0 else status)
    assert list(children.children[0].argv) == cli.headless_command(["--input", "example.py"])


def test_source_checkout_requires_project_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "src" / "entrypoints" / "launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("# fixture\n")
    monkeypatch.setattr(cli, "__file__", str(launcher))
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "vibesys"\n')
    assert cli.source_checkout_root() == tmp_path

    (tmp_path / "pyproject.toml").write_text('[project]\nname = "other"\n')
    assert cli.source_checkout_root() is None
