"""Tests for the interactive server composition entrypoint."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis.strategies import integers
from tests.entrypoints.support import (
    BUDGET_POLLS,
    GATEWAY_PID,
    IDLE_DIRECTORY,
    INSTANCE_PATH,
    FakeDetachedGateway,
    gateway_record,
)

import entrypoints.server as server_entrypoint
import server.runtime as runtime_module
from entrypoints.server import (
    GATEWAY_STOP_TIMEOUT_SECONDS,
    GatewayStopOutcome,
    GatewayStopResult,
    _control_socket_from_argv,
    _DetachedGatewayEffects,
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


class RecordingDetachedEffects(_DetachedGatewayEffects):
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
    with os.fdopen(descriptor, "w+b") as output:
        assert WebInstanceHold.take(descriptor) is True
        running = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        effects = RecordingDetachedEffects(RecordingDetachedProcess(status=None), times=[])

        with pytest.raises(RuntimeError, match="Another process holds") as failure:
            _spawn_detached(["--web", "--detach"], instance_path, effects)
    finally:
        running.terminate()
        running.wait()

    # The lock is taken before the log is truncated, so a second launch cannot
    # destroy the running gateway's output or spawn a rival child. The blocker
    # need not be a gateway and need not have published a record, so the
    # message names the measured holder rather than assuming `stop` can help.
    assert log_path.read_text() == "output from the gateway that is still running\n"
    assert effects.command == []
    assert f"open under {instance_path.parent}: {running.pid}" in str(failure.value)


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

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # The regression: the instance record disappears as the first step of the
    # gateway's teardown, so a stop that returns while the files are still open
    # hands the caller a directory the gateway is still using.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, GATEWAY_PID)
    assert gateway.signals == [GATEWAY_PID]
    assert gateway.observations == 5
    assert gateway.sleeps == [0.05] * 4


def test_stop_detached_gateway_waits_out_a_gateway_that_already_dropped_its_record() -> None:
    gateway = FakeDetachedGateway(recorded_pid=None, already_stopping=True, polls_before_release=2)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # The record is already gone, so there is no pid to signal, but the files
    # are still open: reporting "nothing is running" here is what let a caller
    # remove the directory under a live gateway.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, None)
    assert gateway.signals == []
    assert gateway.observations == 3
    assert gateway.sleeps == [0.05, 0.05]


def test_stop_detached_gateway_stops_a_gateway_that_holds_no_startup_log() -> None:
    gateway = FakeDetachedGateway(log_locked=False, polls_before_release=2)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # A gateway started without `--detach` writes no startup log, so the lock
    # says nothing about it. Deciding on the lock before reading the record
    # reported that live gateway as "not running" and left it running.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, GATEWAY_PID)
    assert gateway.signals == [GATEWAY_PID]


def test_stop_detached_gateway_will_not_signal_a_pid_that_does_not_hold_the_files() -> None:
    gateway = FakeDetachedGateway(
        recorded_pid=9999, holder_pids=(GATEWAY_PID,), polls_before_release=None
    )

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # A record left behind by a killed gateway can name a pid the kernel has
    # since reused. Signalling it terminates an unrelated process, leaves the
    # real holder running, and then reports the wrong pid to the operator.
    assert result.outcome is GatewayStopOutcome.STILL_HOLDING
    assert result.pid is None
    assert result.hold.holders == (GATEWAY_PID,)
    assert gateway.signals == []


def test_stop_detached_gateway_signals_a_pid_it_cannot_check() -> None:
    gateway = FakeDetachedGateway(holder_pids=None, polls_before_release=1)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # Where per-process descriptors are not observable the recorded pid cannot
    # be checked. Leaving the gateway running is the worse failure, so it is
    # signalled unchecked and the lock alone decides when to report success.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, GATEWAY_PID)
    assert gateway.signals == [GATEWAY_PID]


def test_stop_detached_gateway_signals_a_record_that_appears_during_teardown() -> None:
    gateway = FakeDetachedGateway(record_after_polls=2, polls_before_release=1)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # The record is re-read on every poll, so a gateway that publishes late,
    # or republishes a corrected record, is still signalled.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, GATEWAY_PID)
    assert gateway.signals == [GATEWAY_PID]


def test_stop_detached_gateway_reports_a_directory_whose_state_it_cannot_read() -> None:
    gateway = FakeDetachedGateway(
        recorded_pid=None, holder_pids=(), log_locked=None, polls_before_release=None
    )

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # Nothing is visible and the lock state is unreadable, which is what a
    # gateway owned by another user looks like. Reporting success there is the
    # same defect as reporting it over a gateway this host can see.
    assert result.outcome is GatewayStopOutcome.STILL_HOLDING
    assert result.hold == WebInstanceHold(holders=(), log_locked=None)


@given(polls_before_release=integers(min_value=0, max_value=400))
@example(polls_before_release=BUDGET_POLLS - 1)
@example(polls_before_release=BUDGET_POLLS)
def test_stop_detached_gateway_reports_success_only_once_the_files_are_released(
    polls_before_release: int,
) -> None:
    gateway = FakeDetachedGateway(polls_before_release=polls_before_release)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # The property the fix turns on: anything other than a reported failure
    # means nothing observable is using the directory, for any teardown length
    # on either side of the stop budget. Teardown here starts on the signal, so
    # it needs one wait more than it has polls to survive. The explicit
    # examples pin the boundary, which the generated sample steps over.
    waits_needed = polls_before_release + 1
    assert (result.outcome is GatewayStopOutcome.STILL_HOLDING) is not gateway.last_hold.free
    assert result.hold == gateway.last_hold
    assert gateway.sleeps == [0.05] * min(waits_needed, BUDGET_POLLS)
    assert (result.outcome is GatewayStopOutcome.STOPPED) is (waits_needed <= BUDGET_POLLS)


@given(polls_before_release=integers(min_value=0, max_value=400))
@example(polls_before_release=BUDGET_POLLS)
@example(polls_before_release=BUDGET_POLLS + 1)
def test_stop_detached_gateway_never_reports_success_without_a_record(
    polls_before_release: int,
) -> None:
    gateway = FakeDetachedGateway(
        recorded_pid=None, already_stopping=True, polls_before_release=polls_before_release
    )

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # Same property with no record to read, which is the state the gateway is
    # in for almost all of its teardown, and with no pid to signal. Teardown is
    # already under way, so the waits it needs equal its polls, and a directory
    # that is free on the first look is reported as never having been in use.
    assert (result.outcome is GatewayStopOutcome.STILL_HOLDING) is not gateway.last_hold.free
    assert gateway.sleeps == [0.05] * min(polls_before_release, BUDGET_POLLS)
    assert (result.outcome is GatewayStopOutcome.STOPPED) is (
        0 < polls_before_release <= BUDGET_POLLS
    )
    assert (result.outcome is GatewayStopOutcome.NOT_RUNNING) is (polls_before_release == 0)
    assert gateway.signals == []


def test_stop_detached_gateway_reports_a_gateway_that_ignores_termination() -> None:
    gateway = FakeDetachedGateway(polls_before_release=None)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    assert result.outcome is GatewayStopOutcome.STILL_HOLDING
    assert result.pid == GATEWAY_PID
    assert result.hold.holders == (GATEWAY_PID,)
    assert gateway.signals == [GATEWAY_PID]
    # The budget is spent exactly: the last wait the budget allows is taken, and
    # the comparison that ends the loop must not grant one beyond it.
    assert gateway.monotonic() == GATEWAY_STOP_TIMEOUT_SECONDS
    assert gateway.sleeps == [0.05] * BUDGET_POLLS


def test_stop_detached_gateway_reports_an_unused_instance_directory() -> None:
    gateway = FakeDetachedGateway(recorded_pid=None, holder_pids=(), log_locked=False)

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    assert result == GatewayStopResult(GatewayStopOutcome.NOT_RUNNING, IDLE_DIRECTORY, None)
    assert gateway.signals == []
    assert gateway.observations == 1
    assert gateway.sleeps == []


def test_stop_detached_gateway_waits_out_a_gateway_that_was_already_gone() -> None:
    gateway = FakeDetachedGateway(
        signal_error=ProcessLookupError(GATEWAY_PID), already_stopping=True, polls_before_release=1
    )

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # The pid is gone but a descendant still has the inherited log, so there is
    # something to wait for and nothing to signal. Claiming the signal landed
    # would name a pid the operator cannot act on.
    assert result == GatewayStopResult(GatewayStopOutcome.STOPPED, IDLE_DIRECTORY, None)


def test_stop_detached_gateway_reports_a_gateway_it_is_not_allowed_to_signal() -> None:
    gateway = FakeDetachedGateway(
        signal_error=PermissionError(GATEWAY_PID), polls_before_release=None
    )

    result = stop_detached_gateway(INSTANCE_PATH, gateway)

    # A gateway owned by another user cannot be signalled, so it never starts
    # tearing down. Reporting `STOPPED` with its pid, as absorbing the error
    # did, claimed a teardown that was never asked for.
    assert result.outcome is GatewayStopOutcome.STILL_HOLDING
    assert result.pid is None
    assert gateway.signals == []


def test_detached_gateway_effects_read_a_record_and_observe_without_probing(
    tmp_path: Path,
) -> None:
    effects = _DetachedGatewayEffects()
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    record = gateway_record(os.getpid())
    record.write(instance_path)

    # `read_record` answers for a gateway that no longer serves, which
    # `discover` cannot, and `observe` answers without consulting either.
    assert effects.read_record(instance_path) == record
    assert effects.observe(instance_path).free is True

    descriptor = os.open(WebInstanceHold.log_path(instance_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        assert WebInstanceHold.take(descriptor) is True
        assert effects.observe(instance_path) == WebInstanceHold(holders=(), log_locked=True)
    finally:
        os.close(descriptor)

    assert effects.observe(instance_path).free is True


def test_detached_launch_effects_capture_a_real_child_and_use_discovery(tmp_path: Path) -> None:
    effects = _DetachedGatewayEffects()
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
