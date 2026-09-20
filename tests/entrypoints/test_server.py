"""Tests for the interactive server composition entrypoint."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import entrypoints.server as server_entrypoint
from entrypoints.server import _control_socket_from_argv, _headless_argv, main


def test_control_socket_argument_forms() -> None:
    assert _control_socket_from_argv(["--control-socket="]) is None
    assert _control_socket_from_argv(["--control-socket", "control.sock"]) == Path("control.sock")
    assert _headless_argv(["--local", "--control-socket", "control.sock"]) == ["--local"]
    assert _headless_argv(["--control-socket=control.sock", "--local"]) == ["--local"]
    assert _headless_argv(["--theme", "dark", "--local"]) == ["--local"]


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

    The server no longer dispatches through `headless.dispatch`: it parses
    the CLI invocation, builds the `RunRequest` via `headless.build_run_request`,
    and runs it through `ServerRuntime.drive`, which owns the `create_session`
    call (and the core `LocalRunIntegration`) internally.
    """
    import server.runtime as runtime_module  # noqa: PLC0415

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

        def run(self, callback):  # noqa: ANN001, ANN202
            observed["result"] = callback()

        def drive(self, driven_request):  # noqa: ANN001, ANN202
            observed["driven_request"] = driven_request

    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)
    monkeypatch.setattr(server_entrypoint.headless, "parse_cli_invocation", parse_cli_invocation)
    monkeypatch.setattr(server_entrypoint.headless, "build_run_request", build_run_request)
    socket_path = tmp_path / "control.sock"

    main(["--theme", "light", "--local", "--control-socket", str(socket_path)])

    assert observed["socket_path"] == socket_path
    assert callable(observed["tui_defaults"])
    parse_cli_invocation.assert_called_once_with(["--local"])
    build_run_request.assert_called_once_with(invocation)
    assert observed["driven_request"] is request
