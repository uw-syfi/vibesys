"""Tests for the interactive server composition entrypoint."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from tests.server.support import finished_run

import entrypoints.server as server_entrypoint
import server.runtime as runtime_module
from entrypoints.server import (
    _control_socket_from_argv,
    _headless_argv,
    _read_only_log_from_argv,
    _web_assets_from_argv,
    _web_instance_from_argv,
    _web_origins_from_argv,
    _web_port_from_argv,
    _web_requested,
    main,
)
from server.transport.discovery import WebInstanceClaim, WebInstanceRecord
from vibesys.api import ConfigurationError, RunRecord

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


def test_web_reopen_accepts_a_log_directory_or_run_events_file(tmp_path: Path) -> None:
    log_dir = tmp_path / "run"
    events = log_dir / "run-events.jsonl"

    assert _read_only_log_from_argv([]) is None
    assert _read_only_log_from_argv(["--web-reopen", str(log_dir)]) == log_dir
    assert _read_only_log_from_argv(["--web-reopen", str(events)]) == log_dir


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
    with pytest.raises(ValueError, match="requires an origin"):
        _web_origins_from_argv(["--web-origin"])

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


def test_web_requested_accepts_reopen_flags_in_both_forms() -> None:
    assert _web_requested(["--web-reopen-run", "queue-run"]) is True
    assert _web_requested(["--web-reopen-run=queue-run"]) is True
    assert _web_requested(["--web-reopen=run-events.jsonl"]) is True
    assert _web_requested(["--local"]) is False


def test_web_reopen_run_attaches_the_recorded_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, log_dir = finished_run(tmp_path / "project")
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            del socket_path
            observed.update(options)

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    # test-isolation: replace the dynamic runtime import to observe reopen wiring without serving
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)

    main(["--web-reopen-run", run_id, "--project", str(project.root)])

    record = observed["read_only_record"]
    assert isinstance(record, RunRecord)
    assert record.run_id == run_id
    assert observed["read_only_log"] == log_dir


def _refuse_launch(*_args: object, **_kwargs: object) -> None:
    pytest.fail("reopen must fail before launching a gateway")


def test_web_reopen_run_rejects_an_unknown_run_before_launching(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _run_id, _log_dir = finished_run(tmp_path / "project")
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="does not exist"):
        main(
            ["--web", "--detach", "--web-reopen-run", "missing-run", "--project", str(project.root)]
        )


def test_web_reopen_run_rejects_an_unsafe_run_id_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _run_id, _log_dir = finished_run(tmp_path / "project")
    # test-isolation: discovery would create lock files named after the run id
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", _refuse_launch)

    with pytest.raises(ConfigurationError, match="Invalid VibeSys run ID"):
        main(["--web", "--detach", "--web-reopen-run", "../escape", "--project", str(project.root)])
    assert list(tmp_path.rglob("*web-gateway*")) == []


def test_web_reopen_run_requires_the_recorded_event_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, log_dir = finished_run(tmp_path / "project")
    (log_dir / "run-events.jsonl").unlink()
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="No event journal to reopen"):
        main(["--web", "--detach", "--web-reopen-run", run_id, "--project", str(project.root)])


def test_web_reopen_run_rejects_another_runs_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _logs = finished_run(tmp_path / "a")
    _other, _other_id, other_logs = finished_run(tmp_path / "b", run_id="other-run")
    # test-isolation: a detached launch is the side effect this failure must prevent
    monkeypatch.setattr(server_entrypoint, "_spawn_detached", _refuse_launch)

    with pytest.raises(ConfigurationError, match="belongs to run other-run"):
        main(
            [
                "--web",
                "--detach",
                "--web-reopen-run",
                run_id,
                "--web-reopen",
                str(other_logs),
                "--project",
                str(project.root),
            ]
        )


def _tree(root: Path) -> dict[Path, bytes | None]:
    """Every file and directory under *root*, with file contents."""
    return {path: path.read_bytes() if path.is_file() else None for path in root.rglob("*")}


@pytest.mark.parametrize("journal_exists", [True, False])
def test_web_reopen_run_resolves_the_log_directory_without_preparing_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, journal_exists: bool
) -> None:
    """An absent state home stays absent (or unchanged) whether the journal exists or not.

    Regression for the implicit-resolution path (no ``--web-reopen``):
    `project.state.log_directory` prepares the state home as a side effect of
    computing a path; resolving a reopen must use a non-creating equivalent.
    """
    project, run_id, log_dir = finished_run(tmp_path / "project")
    state_home = Path(os.environ["VIBESYS_STATE_HOME"])
    preserved = _tree(log_dir) if journal_exists else {}
    shutil.rmtree(state_home)
    if journal_exists:
        # Recreate only the run's own log directory, as if it were restored
        # onto a machine that never prepared machine-local state.
        for path, contents in preserved.items():
            if contents is None:
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            del socket_path, options

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    # test-isolation: replace the dynamic runtime import to observe reopen wiring without serving
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)

    if journal_exists:
        before = _tree(state_home)
        main(["--web-reopen-run", run_id, "--project", str(project.root)])
        assert _tree(state_home) == before
    else:
        with pytest.raises(ConfigurationError, match="No event journal to reopen"):
            main(["--web-reopen-run", run_id, "--project", str(project.root)])
        assert not state_home.exists()


def test_web_reopen_run_rejects_an_empty_value_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # test-isolation: discovery would create lock files named after the run id
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", _refuse_launch)

    with pytest.raises(ConfigurationError, match="requires a non-empty run ID"):
        main(["--web", "--detach", "--web-reopen-run=", "--project", str(tmp_path)])


def test_web_reopen_run_rejects_a_missing_value_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # test-isolation: discovery would create lock files named after the run id
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", _refuse_launch)

    with pytest.raises(ConfigurationError, match="requires a non-empty run ID"):
        main(["--web", "--detach", "--project", str(tmp_path), "--web-reopen-run"])


def test_reopen_instance_records_are_run_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)

    live = _web_instance_from_argv(["--web"])
    by_run = _web_instance_from_argv(["--web-reopen-run", "queue-run"])
    sibling_a = _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-a")])
    sibling_b = _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-b")])

    assert live == project.resolve() / ".vibesys" / "web-gateway.json"
    assert by_run.name == "web-gateway-queue-run.json"
    assert sibling_a.name.startswith("web-gateway-log-")
    assert sibling_a == _web_instance_from_argv(["--web-reopen", str(tmp_path / "run-a")])
    assert len({live, by_run, sibling_a, sibling_b}) == 4


def test_reopen_never_reuses_the_live_project_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    discovered: list[Path] = []
    spawned: list[Path] = []

    def discover(path: Path) -> None:
        discovered.append(path)

    # test-isolation: observe which record the launcher consults instead of probing a real gateway
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", discover)
    # test-isolation: observe the detached launch instead of starting a child process
    monkeypatch.setattr(
        server_entrypoint, "_spawn_detached", lambda _arguments, path: spawned.append(path)
    )

    main(["--web", "--detach", "--web-reopen-run", run_id, "--project", str(project.root)])

    expected = (project.configuration_path() / f"web-gateway-{run_id}.json").resolve()
    assert discovered == [expected]
    assert spawned == [expected]
    assert expected != (project.configuration_path() / "web-gateway.json").resolve()


def test_launcher_refuses_to_reuse_a_gateway_serving_another_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    live = WebInstanceRecord(
        pid=123,
        port=43_211,
        token=f"{'capability'}-{'token'}",
        url="http://127.0.0.1:43211/?token=capability-token",
        project_root=str(project.root),
        started_at=1.0,
    )
    opened: list[str] = []
    # test-isolation: inject a live gateway at the explicitly shared record path
    monkeypatch.setattr(server_entrypoint, "_discover_web_instance", lambda _path: live)
    # test-isolation: capture browser opening so a wrong reuse is observable and headless
    monkeypatch.setattr(
        server_entrypoint.webbrowser, "open", lambda url, **_kwargs: opened.append(url)
    )

    with pytest.raises(ConfigurationError, match="held by a live gateway"):
        main(
            [
                "--web",
                "--web-instance",
                str(tmp_path / "shared.json"),
                "--web-reopen-run",
                run_id,
                "--project",
                str(project.root),
            ]
        )
    assert opened == []


def test_web_runtime_records_the_requested_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, run_id, _log_dir = finished_run(tmp_path / "project")
    observed: dict[str, object] = {}

    class FakeRuntime:
        def __init__(self, *, socket_path: Path, **options: object) -> None:
            del socket_path
            observed.update(options)

        def run(self, callback: Callable[[], object]) -> object:
            return callback()

    monkeypatch.setenv("VIBESYS_DETACHED_CHILD", "1")
    # test-isolation: replace the dynamic runtime import to observe gateway metadata wiring
    monkeypatch.setattr(runtime_module, "ServerRuntime", FakeRuntime)

    main(["--web-reopen-run", run_id, "--project", str(project.root)])

    assert observed["project_root"] == project.root.resolve()


def test_web_launch_with_a_missing_project_is_a_configuration_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="Project root does not exist"):
        main(["--web", "--project", str(tmp_path / "missing")])
