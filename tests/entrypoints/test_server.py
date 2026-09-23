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
    _web_instance_from_argv,
    _web_origins_from_argv,
    _web_port_from_argv,
    _web_requested,
    main,
)
from server.transport.discovery import WebInstanceClaim, WebInstanceRecord

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
    assert _headless_argv(
        [
            "--web",
            "--web-port",
            "4312",
            "--web-assets",
            "dist",
            "--web-origin",
            "http://127.0.0.1:5173",
            "--local",
        ]
    ) == ["--local"]


def test_web_port_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError, match="between 0 and 65535"):
        _web_port_from_argv(["--web-port", "65536"])


def test_web_port_and_asset_parsers_cover_invalid_and_explicit_values(tmp_path: Path) -> None:
    assert _web_port_from_argv([]) == 0
    with pytest.raises(ValueError, match="must be an integer"):
        _web_port_from_argv(["--web-port", "not-a-port"])
    asset_dir = tmp_path / "dist"
    assert _web_assets_from_argv(["--web-assets", str(asset_dir)]) == asset_dir.resolve()


def test_web_origins_accept_repeated_explicit_origins() -> None:
    assert _web_origins_from_argv(
        [
            "--web-origin",
            "http://127.0.0.1:5173",
            "--web-origin=https://localhost:5173/",
            "--web-origin",
            "http://127.0.0.1:5173",
        ]
    ) == ("http://127.0.0.1:5173", "https://localhost:5173")


def test_web_origins_reject_paths() -> None:
    with pytest.raises(ValueError, match="without a path"):
        _web_origins_from_argv(["--web-origin", "http://localhost:5173/app"])

    with pytest.raises(ValueError, match="without a path"):
        _web_origins_from_argv(["--web-origin", "http://localhost:5173/?token=bad"])


def test_web_instance_record_is_project_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    monkeypatch.chdir(project_a)
    instance_a = _web_instance_from_argv(["--web"])
    monkeypatch.chdir(project_b)
    instance_b = _web_instance_from_argv(["--web"])

    assert instance_a == project_a / ".vibesys" / "web-gateway.json"
    assert instance_b == project_b / ".vibesys" / "web-gateway.json"
    assert instance_a != instance_b


def test_second_web_launch_reuses_live_instance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    record = WebInstanceRecord(
        pid=123,
        port=43_211,
        token="capability",  # noqa: S106  # lint-waiver: LW-101063 [S106]; use a fixed capability token in a launch-reuse fixture
        url="http://127.0.0.1:43211/?token=capability",
        project_root=str(tmp_path),
        started_at=1.0,
    )
    opened: list[str] = []
    # test-isolation: inject the discovered live gateway to exercise reuse without a real launcher
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", lambda _path: record)
    # test-isolation: capture browser opening so the test remains headless and deterministic
    monkeypatch.setattr(
        server_entrypoint.webbrowser, "open", lambda url, **_kwargs: opened.append(url)
    )

    main(["--web", "--web-instance", str(instance_path)])

    assert capsys.readouterr().out == f"VibeSys web UI: {record.url}\n"
    assert opened == [record.url]


def test_discover_web_instance_waits_for_a_claimed_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    record = WebInstanceRecord(
        pid=123,
        port=43_211,
        token="capability",  # noqa: S106  # lint-waiver: LW-101064 [S106]; use a fixed capability token in a launch-race fixture
        url="http://127.0.0.1:43211/?token=capability",
        project_root=str(tmp_path),
        started_at=1.0,
    )
    discoveries = iter([None, record])
    # test-isolation: replace discovery with a deterministic bind-to-record race
    monkeypatch.setattr(
        server_entrypoint.WebInstanceRecord,
        "discover",
        lambda *_args, **_kwargs: next(discoveries),
    )
    # test-isolation: hold the startup claim while the competing gateway publishes its record
    monkeypatch.setattr(server_entrypoint.WebInstanceClaim, "is_held", lambda _path: True)
    # test-isolation: avoid delaying this deterministic polling test
    monkeypatch.setattr(server_entrypoint.time, "sleep", lambda _seconds: None)
    # test-isolation: keep the race test on the first polling iteration
    monkeypatch.setattr(server_entrypoint.time, "monotonic", lambda: 0.0)

    assert (
        server_entrypoint._discover_web_instance(  # noqa: SLF001  # lint-waiver: LW-101066 [SLF001]; exercise the launch race helper directly
            instance_path
        )
        == record
    )


def test_discover_web_instance_falls_back_after_claim_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    record = WebInstanceRecord(
        pid=123,
        port=43_211,
        token="capability",  # noqa: S106  # lint-waiver: LW-101065 [S106]; use a fixed capability token in a launch-race fixture
        url="http://127.0.0.1:43211/?token=capability",
        project_root=str(tmp_path),
        started_at=1.0,
    )
    discoveries = iter([None, record])
    monotonic_values = iter([0.0, 3.0])
    # test-isolation: replace discovery with a deterministic timeout fallback
    monkeypatch.setattr(
        server_entrypoint.WebInstanceRecord,
        "discover",
        lambda *_args, **_kwargs: next(discoveries),
    )
    # test-isolation: keep the competing startup claim held until the deadline
    monkeypatch.setattr(server_entrypoint.WebInstanceClaim, "is_held", lambda _path: True)
    # test-isolation: advance directly from the deadline setup to the timeout check
    monkeypatch.setattr(server_entrypoint.time, "monotonic", lambda: next(monotonic_values))

    assert (
        server_entrypoint._discover_web_instance(  # noqa: SLF001  # lint-waiver: LW-101067 [SLF001]; exercise the launch timeout fallback directly
            instance_path
        )
        == record
    )


def test_web_instance_claim_reports_ownership(tmp_path: Path) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    claim = WebInstanceClaim(instance_path)

    assert WebInstanceClaim.is_held(instance_path) is False
    assert claim.try_acquire() is True
    assert WebInstanceClaim.is_held(instance_path) is True
    claim.close()
    assert WebInstanceClaim.is_held(instance_path) is False


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

    class ReturnValue:
        def __init__(self, value: object) -> None:
            self.value = value

        def __call__(self, *_args: object, **_kwargs: object) -> object:
            return self.value

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            observed["socket_path"] = socket_path
            observed["options"] = options

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

        def drive(self, driven_request: object) -> None:
            observed["request"] = driven_request

    # test-isolation: replace the dynamic runtime import with a local fake to test web wiring
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)
    # test-isolation: replace CLI parsing with a deterministic return-value fake
    monkeypatch.setattr(server_entrypoint.cli, "parse_cli_invocation", ReturnValue(invocation))
    # test-isolation: replace request construction with a deterministic return-value fake
    monkeypatch.setattr(server_entrypoint.cli, "build_run_request", ReturnValue(request))

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
