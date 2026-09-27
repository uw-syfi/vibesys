"""Tests for the interactive server composition entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

import entrypoints.server as server_entrypoint
import server.runtime as runtime_module
from entrypoints.server import (
    _control_socket_from_argv,
    _headless_argv,
    _web_assets_from_argv,
    _web_port_from_argv,
    _web_requested,
    main,
)

if TYPE_CHECKING:
    from collections.abc import Callable


def test_control_socket_argument_forms() -> None:
    assert _control_socket_from_argv(["--control-socket="]) is None
    assert _control_socket_from_argv(["--control-socket", "control.sock"]) == Path("control.sock")
    assert _headless_argv(["--local", "--control-socket", "control.sock"]) == ["--local"]
    assert _headless_argv(["--control-socket=control.sock", "--local"]) == ["--local"]
    assert _headless_argv(["--theme", "dark", "--local"]) == ["--local"]


def test_web_server_arguments_are_consumed_before_run_parsing() -> None:
    assert _web_requested(["--web", "--web-port", "4312"]) is True
    assert _web_port_from_argv(["--web-port=4312"]) == 4312
    assert _headless_argv(["--web", "--web-port", "4312", "--web-assets", "dist", "--local"]) == [
        "--local"
    ]


def test_web_port_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="between 0 and 65535"):
        _web_port_from_argv(["--web-port", "65536"])


def test_web_port_and_asset_parsers_cover_invalid_and_explicit_values(tmp_path: Path) -> None:
    assert _web_port_from_argv([]) == 0
    with pytest.raises(ValueError, match="must be an integer"):
        _web_port_from_argv(["--web-port", "not-a-port"])
    asset_dir = tmp_path / "dist"
    assert _web_assets_from_argv(["--web-assets", str(asset_dir)]) == asset_dir.resolve()


def test_tui_defaults_use_launch_config_and_normalize_runs_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "agent.toml").write_text(
        '[model]\nname = "gpt-5.5"\n'
        '[repository]\nowner = "my-lab"\nvisibility = "private"\n'
        '[tui]\ntheme = "high-contrast-dark"\n'
    )
    monkeypatch.chdir(tmp_path)

    main(["tui-defaults", "--runs-dir", "runs"])

    defaults = json.loads(capsys.readouterr().out)
    assert defaults["runs_dir"] == str((tmp_path / "runs").resolve())
    assert defaults["repository_owner"] == "my-lab"
    assert defaults["theme"] == "high-contrast-dark"


def test_tui_defaults_reject_a_missing_explicit_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(SystemExit) as exc:
        main(["tui-defaults", "--config", str(missing)])
    assert exc.value.code == 2
    assert str(missing) in capsys.readouterr().err


def test_server_runtime_drives_the_built_run_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`main` builds the `RunRequest` itself and drives it on `ServerRuntime`.

    The server no longer dispatches through `entrypoints.cli.dispatch`: it
    parses the CLI invocation, builds the `RunRequest` via
    `entrypoints.cli.build_run_request`, and runs it through
    `ServerRuntime.drive`, which owns the `create_session` call (and the core
    `LocalRunIntegration`) internally.
    """
    integration = object()
    invocation = object()
    request = object()
    parse_cli_invocation = Mock(return_value=invocation)
    build_run_request = Mock(return_value=request)
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, tui_defaults: object) -> None:
            observed["socket_path"] = socket_path
            observed["tui_defaults"] = tui_defaults
            self.integration = integration

        def run(self, callback: Callable[[], object]) -> None:
            observed["result"] = callback()

        def drive(self, driven_request: object) -> None:
            observed["driven_request"] = driven_request

    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)
    monkeypatch.setattr(server_entrypoint.cli, "parse_cli_invocation", parse_cli_invocation)
    monkeypatch.setattr(server_entrypoint.cli, "build_run_request", build_run_request)
    socket_path = tmp_path / "control.sock"

    main(["--theme", "light", "--local", "--control-socket", str(socket_path)])

    assert observed["socket_path"] == socket_path
    assert callable(observed["tui_defaults"])
    parse_cli_invocation.assert_called_once_with(["--local"])
    build_run_request.assert_called_once_with(invocation)
    assert observed["driven_request"] is request


def test_web_main_uses_ephemeral_socket_and_web_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    invocation = object()
    request = object()

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            observed["socket_path"] = socket_path
            observed["options"] = options

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

        def drive(self, driven_request: object) -> None:
            observed["request"] = driven_request

    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)
    monkeypatch.setattr(server_entrypoint.cli, "parse_cli_invocation", Mock(return_value=invocation))
    monkeypatch.setattr(server_entrypoint.cli, "build_run_request", Mock(return_value=request))

    main(["--web", "--web-port", "4312", "--web-assets", str(tmp_path), "--local"])

    assert isinstance(observed["socket_path"], Path)
    assert observed["socket_path"].name == "control.sock"
    assert observed["socket_path"].parent.name.startswith("vibesys-web-")
    options = observed["options"]
    assert isinstance(options, dict)
    assert options["web"] is True
    assert options["web_port"] == 4312
    assert options["web_assets"] == tmp_path.resolve()
    assert callable(options["tui_defaults"])
    assert observed["request"] is request
