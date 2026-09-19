"""Interactive setup defaults exposed through the server API."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.server.support import build_server_parts

from entrypoints.server import _tui_defaults_from_argv
from server.settings import TuiTheme
from server.wire import enums, messages
from server.wire.v2 import responses_pb2

if TYPE_CHECKING:
    from pathlib import Path


def _theme(theme: TuiTheme) -> responses_pb2.TuiTheme:
    return enums.number(responses_pb2.TuiTheme, theme)


def _defaults(theme: TuiTheme) -> responses_pb2.TuiDefaults:
    return responses_pb2.TuiDefaults(
        runs_dir="/runs",
        input_path="",
        experiment_name="experiment-1",
        repository_owner=None,
        repository_name="experiment-1",
        visibility=responses_pb2.REPOSITORY_VISIBILITY_PRIVATE,
        theme=_theme(theme),
    )


def test_api_answers_with_provider_defaults() -> None:
    parts = build_server_parts(tui_defaults=lambda: _defaults(TuiTheme.SOLARIZED_LIGHT))

    response = parts.api.execute(messages.make_request("tui_defaults"))

    assert response.ok
    assert response.HasField("tui_defaults")
    assert response.tui_defaults.theme == _theme(TuiTheme.SOLARIZED_LIGHT)


def test_api_resolves_defaults_on_demand_and_caches_them() -> None:
    calls: list[int] = []

    def provide() -> responses_pb2.TuiDefaults:
        calls.append(1)
        return _defaults(TuiTheme.LIGHT)

    parts = build_server_parts(tui_defaults=provide)
    assert calls == []

    first = parts.api.execute(messages.make_request("tui_defaults"))
    second = parts.api.execute(messages.make_request("tui_defaults"))

    assert calls == [1]
    assert first.tui_defaults == second.tui_defaults


def test_api_without_provider_reports_no_defaults() -> None:
    response = build_server_parts().api.execute(messages.make_request("tui_defaults"))
    assert response.ok
    assert not response.HasField("tui_defaults")


def test_failing_provider_surfaces_as_request_error() -> None:
    def provide() -> responses_pb2.TuiDefaults:
        raise FileNotFoundError("agent.toml is missing")  # noqa: TRY003

    parts = build_server_parts(tui_defaults=provide)
    with pytest.raises(FileNotFoundError):
        parts.api.execute(messages.make_request("tui_defaults"))


def test_provider_resolves_theme_from_launch_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "agent.toml").write_text(
        '[model]\nname = "gpt-5.5"\n[tui]\ntheme = "catppuccin-mocha"\n'
    )
    monkeypatch.chdir(tmp_path)

    defaults = _tui_defaults_from_argv(["--stub-agent", "--headless"])()

    assert defaults.theme == _theme(TuiTheme.CATPPUCCIN_MOCHA)
    assert not defaults.HasField("repository_owner")


def test_provider_honors_explicit_config_and_theme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "elsewhere.toml"
    config.write_text('[model]\nname = "gpt-5.5"\n[tui]\ntheme = "light"\n')
    monkeypatch.chdir(tmp_path)

    from_config = _tui_defaults_from_argv(["--config", str(config)])()
    from_flag = _tui_defaults_from_argv([f"--config={config}", "--theme", "high-contrast-dark"])()

    assert from_config.theme == _theme(TuiTheme.LIGHT)
    assert from_flag.theme == _theme(TuiTheme.HIGH_CONTRAST_DARK)
