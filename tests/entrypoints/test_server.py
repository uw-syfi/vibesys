"""Tests for the interactive server composition entrypoint."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from hypothesis import given
from hypothesis.strategies import integers

import entrypoints.server as server_entrypoint
import server.runtime as runtime_module
from entrypoints.server import (
    GATEWAY_STOP_TIMEOUT_SECONDS,
    GatewayStopOutcome,
    GatewayStopResult,
    _control_socket_from_argv,
    _DetachedLaunchEffects,
    _headless_argv,
    _read_only_log_from_argv,
    _spawn_detached,
    _stop_detached,
    _web_assets_from_argv,
    _web_instance_from_argv,
    _web_origins_from_argv,
    _web_port_from_argv,
    _web_requested,
    main,
    stop_detached_gateway,
)
from server.transport.discovery import WebInstanceClaim, WebInstanceHold, WebInstanceRecord

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import BinaryIO


class RecordingDetachedProcess:
    def __init__(self, status: int | None) -> None:
        self.status = status
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self.status

    def terminate(self) -> None:
        self.terminated = True
        self.status = -15

    def wait(self, timeout: float | None = None) -> int:
        assert timeout == 2.0
        return self.status or 0

    def kill(self) -> None:
        self.killed = True
        self.status = -9


class RecordingDetachedEffects(_DetachedLaunchEffects):
    def __init__(
        self,
        process: RecordingDetachedProcess,
        times: list[float],
        record: WebInstanceRecord | None = None,
    ) -> None:
        self.process = process
        self.times = iter(times)
        self.record = record
        self.command: list[str] = []
        self.environment: dict[str, str] = {}
        self.sleeps: list[float] = []

    def spawn(
        self,
        command: list[str],
        environment: dict[str, str],
        output: BinaryIO,
    ) -> RecordingDetachedProcess:
        self.command = command
        self.environment = environment
        output.write(b"Address already in use\n")
        output.flush()
        return self.process

    def discover(self, instance_path: Path) -> WebInstanceRecord | None:
        assert instance_path.name == "web-gateway.json"
        return self.record

    def monotonic(self) -> float:
        return next(self.times)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _gateway_record(pid: int) -> WebInstanceRecord:
    token = "capability" + "-token"
    return WebInstanceRecord(
        pid=pid,
        port=43_211,
        token=token,
        url=f"http://127.0.0.1:43211/?token={token}",
        project_root="/project",
        started_at=1.0,
    )


_INSTANCE_PATH = Path("/project/.vibesys/web-gateway.json")


class FakeDetachedGateway:
    """An in-memory detached gateway, seen the way an unrelated process sees one.

    It models the two facts a stop request can read, and their real lifetimes.
    The instance record is published while the gateway serves and unlinked as
    the *first* step of teardown, so ``record_published=False`` is the state a
    shutting-down gateway spends most of its teardown in. The hold on the
    instance files is released only once the process and every descendant that
    inherited its log are gone, so it outlives the record: teardown begins on
    SIGTERM, or at construction with ``already_stopping``, and the hold then
    survives ``polls_before_release`` further observations.
    ``polls_before_release=None`` models a gateway that ignores the signal, and
    ``already_stopping`` with no further polls models an instance directory
    nothing is using. The clock advances only when the caller sleeps, so the
    budget is simulated, never waited for.
    """

    def __init__(
        self,
        *,
        record_published: bool = True,
        polls_before_release: int | None = 0,
        already_stopping: bool = False,
        signal_error: Exception | None = None,
    ) -> None:
        self.record = _gateway_record(4321)
        self.record_published = record_published
        self.polls_before_release = polls_before_release
        self.stopping = already_stopping
        self.signal_error = signal_error
        self.signals: list[int] = []
        self.observations = 0
        self.teardown_polls = 0
        self.last_observation = True
        self.sleeps: list[float] = []
        self.clock = 0.0

    def read_record(self, instance_path: Path) -> WebInstanceRecord | None:
        assert instance_path == _INSTANCE_PATH
        return self.record if self.record_published else None

    def instance_held(self, instance_path: Path) -> bool:
        assert instance_path == _INSTANCE_PATH
        self.observations += 1
        self.last_observation = self._held()
        return self.last_observation

    def _held(self) -> bool:
        if self.stopping and self.polls_before_release is not None:
            if self.teardown_polls >= self.polls_before_release:
                return False
            self.teardown_polls += 1
        return True

    def terminate(self, pid: int) -> None:
        self.signals.append(pid)
        self.stopping = True
        if self.signal_error is not None:
            raise self.signal_error

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.clock += seconds


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


def test_web_origins_reject_what_no_browser_can_send() -> None:
    with pytest.raises(ValueError, match="requires an origin"):
        _web_origins_from_argv(["--web-origin"])

    # Every rejection names the flag that carried the value and the value
    # itself, so the operator can find what they typed. Before this, an
    # unparseable authority reached `urlsplit` and surfaced as a bare
    # `ValueError: Invalid IPv6 URL` naming neither.
    rejected = (
        "http://localhost:5173/app",
        "http://localhost:5173/?token=bad",
        "http://[::1",
        "http://*:5173",
        "http://!;evil.example:5173",
        "http://user:pw@localhost:5173",
        "file:///tmp/page.html",
    )
    for value in rejected:
        with pytest.raises(ValueError, match="--web-origin") as failure:
            _web_origins_from_argv(["--web-origin", value])
        assert repr(value) in str(failure.value), value


def test_web_origins_canonicalize_to_the_spelling_a_browser_sends() -> None:
    # The gateway compares `Origin` by exact string, so the launcher stores the
    # form a browser actually sends: lowercase scheme and host, default port
    # omitted. `http://LOCALHOST:5173` would otherwise never match.
    assert _web_origins_from_argv(
        [
            "--web-origin",
            "http://LOCALHOST:5173",
            "--web-origin=HTTPS://Proxy.Example:443",
            "--web-origin",
            "http://[::1]:5173",
        ]
    ) == ("http://localhost:5173", "https://proxy.example", "http://[::1]:5173")


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


def test_detached_startup_surfaces_early_child_failure(tmp_path: Path) -> None:
    process = RecordingDetachedProcess(status=7)
    effects = RecordingDetachedEffects(process, times=[0.0, 0.0])
    instance_path = tmp_path / "web-gateway.json"

    with pytest.raises(RuntimeError, match=r"(?s)exited with status 7.*Address already in use"):
        _spawn_detached(["--web", "--detach"], instance_path, effects)

    assert effects.command[-2:] == ["--web", "--detach"]
    assert effects.environment["VIBESYS_DETACHED_CHILD"] == "1"
    log_path = tmp_path / "web-gateway.json.log"
    assert log_path.read_text() == "Address already in use\n"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert process.terminated is False


def test_detached_startup_refuses_to_clobber_a_gateway_that_still_holds_the_log(
    tmp_path: Path,
) -> None:
    instance_path = tmp_path / "web-gateway.json"
    log_path = WebInstanceHold.log_path(instance_path)
    log_path.write_text("output from the gateway that is still running\n")
    descriptor = os.open(log_path, os.O_RDWR)
    try:
        assert WebInstanceHold.take(descriptor) is True
        effects = RecordingDetachedEffects(RecordingDetachedProcess(status=None), times=[])

        with pytest.raises(RuntimeError, match="still has files open"):
            _spawn_detached(["--web", "--detach"], instance_path, effects)
    finally:
        os.close(descriptor)

    # The hold is taken before the log is truncated, so a second launch cannot
    # destroy the running gateway's output or spawn a rival child.
    assert log_path.read_text() == "output from the gateway that is still running\n"
    assert effects.command == []


def test_detached_startup_holds_the_log_it_hands_to_the_child(tmp_path: Path) -> None:
    instance_path = tmp_path / "web-gateway.json"
    process = RecordingDetachedProcess(status=7)
    effects = RecordingDetachedEffects(process, times=[0.0, 0.0])

    assert WebInstanceHold.is_held(instance_path) is False
    with pytest.raises(RuntimeError, match="exited with status 7"):
        _spawn_detached(["--web", "--detach"], instance_path, effects)

    # The launcher held the log while the child was starting, and released it
    # by closing its own descriptor once startup failed. A real child keeps the
    # same descriptor as stdout and stderr, which is what makes the hold
    # outlive the gateway's teardown.
    assert WebInstanceHold.is_held(instance_path) is False


def test_detached_startup_reports_a_published_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    process = RecordingDetachedProcess(status=None)
    record = WebInstanceRecord(
        pid=123,
        port=43_211,
        token="capability",  # noqa: S106  # lint-waiver: LW-101106 [S106]; use a fixed capability token in a detached-startup fixture
        url="http://127.0.0.1:43211/?token=capability",
        project_root=str(tmp_path),
        started_at=1.0,
    )
    effects = RecordingDetachedEffects(process, times=[0.0, 0.0], record=record)
    opened: list[str] = []
    # test-isolation: keep the startup test headless while verifying the published URL.
    monkeypatch.setattr(
        server_entrypoint.webbrowser, "open", lambda url, **_kwargs: opened.append(url)
    )

    _spawn_detached(["--web", "--detach"], tmp_path / "web-gateway.json", effects)

    assert capsys.readouterr().out == f"VibeSys web UI: {record.url}\n"
    assert opened == [record.url]
    assert process.terminated is False


def test_detached_startup_terminates_a_child_that_never_becomes_ready(tmp_path: Path) -> None:
    process = RecordingDetachedProcess(status=None)
    effects = RecordingDetachedEffects(process, times=[0.0, 0.0, 11.0])

    with pytest.raises(RuntimeError, match=r"(?s)did not become ready.*Address already in use"):
        _spawn_detached(["--web", "--detach"], tmp_path / "web-gateway.json", effects)

    assert process.terminated is True
    assert process.killed is False
    assert effects.sleeps == [0.05]


def test_detached_stop_escalates_only_when_the_child_ignores_termination() -> None:
    stopped = RecordingDetachedProcess(status=0)
    _stop_detached(stopped)
    assert stopped.terminated is False

    class StubbornProcess(RecordingDetachedProcess):
        def wait(self, timeout: float | None = None) -> int:
            assert timeout == 2.0
            if not self.killed:
                raise subprocess.TimeoutExpired("detached-child", 2.0)
            return -9

    stubborn = StubbornProcess(status=None)
    _stop_detached(stubborn)
    assert stubborn.terminated is True
    assert stubborn.killed is True


def test_stop_detached_gateway_waits_until_the_instance_files_are_released() -> None:
    gateway = FakeDetachedGateway(polls_before_release=3)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # The regression: the instance record disappears as the first step of the
    # gateway's teardown, so a stop that returns while the hold is still taken
    # hands the caller a directory the gateway has files open under.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, 4321)
    assert gateway.signals == [4321]
    assert gateway.observations == 5
    assert gateway.sleeps == [0.05, 0.05, 0.05]


def test_stop_detached_gateway_waits_out_a_gateway_that_already_dropped_its_record() -> None:
    gateway = FakeDetachedGateway(
        record_published=False, already_stopping=True, polls_before_release=2
    )

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # The record is already gone, so there is no pid to signal, but the files
    # are still open: reporting "nothing is running" here is what let a caller
    # remove the directory under a live gateway.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, None)
    assert gateway.signals == []
    assert gateway.last_observation is False


def test_stop_detached_gateway_signals_a_gateway_it_cannot_reach_over_http() -> None:
    gateway = FakeDetachedGateway(polls_before_release=1)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # Nothing here probes the gateway's health endpoint, so a gateway that is
    # published but unreachable (wedged, paused, or mid-teardown) is still
    # signalled and still waited for.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, 4321)
    assert gateway.signals == [4321]


@given(polls_before_release=integers(min_value=0, max_value=400))
def test_stop_detached_gateway_reports_success_only_once_the_hold_is_released(
    polls_before_release: int,
) -> None:
    gateway = FakeDetachedGateway(polls_before_release=polls_before_release)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # The property the fix turns on: anything other than a reported failure
    # means the hold is released, for any teardown length on either side of
    # the stop budget.
    assert (result.outcome is GatewayStopOutcome.STILL_HOLDING) is gateway.last_observation
    assert gateway.sleeps == [0.05] * len(gateway.sleeps)
    budget_polls = GATEWAY_STOP_TIMEOUT_SECONDS / 0.05
    assert (result.outcome is GatewayStopOutcome.STOPPED) is (polls_before_release <= budget_polls)


@given(polls_before_release=integers(min_value=0, max_value=400))
def test_stop_detached_gateway_never_reports_success_without_a_record(
    polls_before_release: int,
) -> None:
    gateway = FakeDetachedGateway(
        record_published=False, already_stopping=True, polls_before_release=polls_before_release
    )

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # Same property with no record to read, which is the state the gateway is
    # in for almost all of its teardown, and with no pid to signal.
    assert (result.outcome is GatewayStopOutcome.STILL_HOLDING) is gateway.last_observation
    assert gateway.signals == []


def test_stop_detached_gateway_reports_a_gateway_that_ignores_termination() -> None:
    gateway = FakeDetachedGateway(polls_before_release=None)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    assert result == GatewayStopResult(GatewayStopOutcome.STILL_HOLDING, 4321)
    assert gateway.signals == [4321]
    assert gateway.clock >= GATEWAY_STOP_TIMEOUT_SECONDS
    assert gateway.sleeps == [0.05] * len(gateway.sleeps)


def test_stop_detached_gateway_reports_an_unused_instance_directory() -> None:
    gateway = FakeDetachedGateway(already_stopping=True)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    assert result == GatewayStopResult(GatewayStopOutcome.NOT_RUNNING, None)
    assert gateway.signals == []
    assert gateway.sleeps == []


@pytest.mark.parametrize("failure", [ProcessLookupError(4321), PermissionError(4321)])
def test_stop_detached_gateway_absorbs_a_signal_it_cannot_deliver(failure: Exception) -> None:
    gateway = FakeDetachedGateway(polls_before_release=1, signal_error=failure)

    result = stop_detached_gateway(_INSTANCE_PATH, gateway)

    # A signal that cannot be delivered is not an answer either way, so the
    # hold still decides the outcome and nothing escapes to the operator as a
    # traceback.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, 4321)


def test_detached_launch_effects_read_a_record_and_hold_without_probing(tmp_path: Path) -> None:
    effects = _DetachedLaunchEffects()
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    record = _gateway_record(os.getpid())
    record.write(instance_path)

    assert effects.read_record(instance_path) == record
    assert effects.instance_held(instance_path) is False

    descriptor = os.open(WebInstanceHold.log_path(instance_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert WebInstanceHold.take(descriptor) is True
        assert effects.instance_held(instance_path) is True
    finally:
        os.close(descriptor)

    # `read_record` never probes, so it answers for a gateway that no longer
    # serves, which `discover` cannot: there is no health endpoint here.
    assert effects.discover(instance_path) is None
    assert effects.instance_held(instance_path) is False


def test_detached_launch_effects_capture_a_real_child_and_use_discovery(tmp_path: Path) -> None:
    effects = _DetachedLaunchEffects()
    output_path = tmp_path / "child.log"
    with output_path.open("w+b") as output:
        process = effects.spawn(
            [sys.executable, "-c", "print('captured child output')"],
            dict(os.environ),
            output,
        )
        assert process.wait(timeout=2) == 0

    assert output_path.read_text() == "captured child output\n"
    assert effects.discover(tmp_path / "missing.json") is None
    before = effects.monotonic()
    effects.sleep(0)
    assert effects.monotonic() >= before


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
