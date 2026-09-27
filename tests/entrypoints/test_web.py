"""Tests for the web UI developer and operator helpers."""

from __future__ import annotations

import argparse
import subprocess
from typing import TYPE_CHECKING, cast

import pytest

from entrypoints import web
from entrypoints.web import (
    _DEMO_LOG,
    _browser_url,
    _live_command,
    _local_url,
    _parser,
    _pnpm,
    _port,
    _run_dev,
    _run_live,
    _run_status,
    _run_stop,
    _run_tunnel,
    _ssh,
    _wait_for_record,
)

if TYPE_CHECKING:
    from pathlib import Path


def _record() -> web.WebInstanceRecord:
    token = "test" + "-token"
    return web.WebInstanceRecord(
        pid=1234,
        port=8765,
        token=token,
        url=f"http://127.0.0.1:8765/?token={token}",
        project_root="project-root",
        started_at=0.0,
    )


def test_live_demo_command_uses_the_shared_server_entrypoint(tmp_path: Path) -> None:
    replay_log = tmp_path / "framework-events.jsonl"
    command = _live_command(
        project=None,
        replay_log=replay_log,
        task=None,
        port=8765,
        instance=tmp_path / "web-gateway.json",
        run_args=(),
        browser_origins=(),
    )

    assert command[-2:] == ["--web-reopen", str(replay_log)]
    assert "--stub-agent" not in command
    assert command[1:4] == ["-m", "entrypoints.server", "--web"]


def test_live_command_preserves_arguments_after_separator(tmp_path: Path) -> None:
    command = _live_command(
        project=tmp_path / "project",
        replay_log=None,
        task=None,
        port=8765,
        instance=tmp_path / "web-gateway.json",
        run_args=("--", "--outer-loop", "plain", "--local"),
        browser_origins=("http://127.0.0.1:5173",),
    )

    assert command[-5:] == [
        "--web-origin",
        "http://127.0.0.1:5173",
        "--outer-loop",
        "plain",
        "--local",
    ]


def test_port_accepts_valid_values_and_rejects_invalid_values() -> None:
    assert _port("8765") == 8765

    with pytest.raises(argparse.ArgumentTypeError, match="must be an integer"):
        _port("web")
    with pytest.raises(argparse.ArgumentTypeError, match="between 1 and 65535"):
        _port("65536")


def test_parser_builds_each_browser_workflow() -> None:
    dev = _parser().parse_args(["dev"])
    assert (dev.command, dev.host, dev.port) == ("dev", "127.0.0.1", 5173)

    live = _parser().parse_args(["live", "--demo", "--browser-origin", "http://127.0.0.1:5173"])
    assert (live.command, live.demo, live.browser_origin) == (
        "live",
        True,
        ["http://127.0.0.1:5173"],
    )

    tunnel = _parser().parse_args(["tunnel", "--host", "user@host", "--url", "URL"])
    assert (tunnel.command, tunnel.host, tunnel.browser_origin) == (
        "tunnel",
        "user@host",
        "http://127.0.0.1:5173",
    )

    assert _parser().parse_args(["status", "--instance", "record.json"]).command == "status"
    assert _parser().parse_args(["stop", "--instance", "record.json"]).command == "stop"


def test_tool_lookup_reports_missing_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_executable(_name: str) -> None:
        return None

    # test-isolation: tool lookup has no injected dependency boundary.
    monkeypatch.setattr(web.shutil, "which", missing_executable)
    with pytest.raises(SystemExit, match="pnpm is required"):
        _pnpm()
    with pytest.raises(SystemExit, match="ssh is required"):
        _ssh()


def test_run_dev_invokes_vite_with_the_requested_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 7)

    # test-isolation: subprocess invocation is the behavior under test.
    monkeypatch.setattr(web, "_pnpm", lambda: "pnpm")
    # test-isolation: subprocess invocation is the behavior under test.
    monkeypatch.setattr(web.subprocess, "run", fake_run)

    result = _run_dev(argparse.Namespace(host="127.0.0.1", port=5173), tmp_path)

    assert result == 7
    assert calls[0][0][0] == ["pnpm", "dev", "--host", "127.0.0.1", "--port", "5173"]


def test_wait_for_record_returns_a_published_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _record()

    def discover(path: Path, *, cleanup_stale: bool) -> web.WebInstanceRecord:
        assert path == tmp_path / "record.json"
        assert not cleanup_stale
        return record

    # test-isolation: discovery is replaced to isolate the polling helper.
    monkeypatch.setattr(web.WebInstanceRecord, "discover", discover)

    assert _wait_for_record(tmp_path / "record.json") == record


def test_wait_for_record_reports_a_startup_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def discover(path: Path, *, cleanup_stale: bool) -> None:
        assert path
        assert not cleanup_stale

    # test-isolation: discovery is replaced to exercise the timeout branch.
    monkeypatch.setattr(web.WebInstanceRecord, "discover", discover)
    # test-isolation: shorten the bounded wait for a deterministic timeout test.
    monkeypatch.setattr(web, "_RECORD_WAIT_SECONDS", 0.0)

    with pytest.raises(SystemExit, match="did not publish"):
        _wait_for_record(tmp_path / "record.json")


def test_run_live_launches_gateway_and_prints_browser_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    instance = tmp_path / "record.json"
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 0)

    # test-isolation: live mode delegates to build, launch, and record discovery.
    monkeypatch.setattr(web, "_pnpm", lambda: "pnpm")
    # test-isolation: live mode delegates to build, launch, and record discovery.
    monkeypatch.setattr(web, "_live_command", lambda **_kwargs: ["gateway"])
    # test-isolation: live mode delegates to build, launch, and record discovery.
    monkeypatch.setattr(web, "_wait_for_record", lambda _path: _record())
    # test-isolation: live mode delegates to build, launch, and record discovery.
    monkeypatch.setattr(web.subprocess, "run", fake_run)

    args = argparse.Namespace(
        project=project,
        task="spsc",
        port=8765,
        instance=instance,
        ssh_target="user@host",
        browser_origin=["http://127.0.0.1:5173"],
        demo=False,
        no_build=False,
        open=False,
        run_args=(),
    )

    assert _run_live(args, tmp_path) == 0
    assert len(calls) == 2
    assert calls[0][0][0] == ["pnpm", "build"]
    assert calls[1][0][0] == ["gateway"]
    output = capsys.readouterr().out
    assert "SSH tunnel:" in output
    assert "Browser harness URL:" in output


def test_run_live_rejects_missing_project_for_non_demo_mode(tmp_path: Path) -> None:
    args = argparse.Namespace(
        project=None,
        task=None,
        port=8765,
        instance=None,
        ssh_target=None,
        browser_origin=[],
        demo=False,
        no_build=True,
        open=False,
        run_args=(),
    )
    with pytest.raises(SystemExit, match="pass --project"):
        _run_live(args, tmp_path)


def test_run_live_demo_stages_replay_log_for_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    replay_source = tmp_path / _DEMO_LOG
    replay_source.parent.mkdir(parents=True)
    replay_source.write_text('{"type":"run_finished"}\n')
    captured: dict[str, object] = {}

    def live_command(**kwargs: object) -> list[str]:
        captured.update(kwargs)
        return ["gateway"]

    # test-isolation: inspect gateway composition without starting a subprocess.
    monkeypatch.setattr(web, "_live_command", live_command)
    # test-isolation: isolate the helper from a detached server process.
    monkeypatch.setattr(
        web.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0),
    )
    # test-isolation: return the record a real gateway publishes after startup.
    monkeypatch.setattr(web, "_wait_for_record", lambda _path: _record())
    args = argparse.Namespace(
        project=None,
        task=None,
        port=8765,
        instance=None,
        ssh_target=None,
        browser_origin=[],
        demo=True,
        no_build=True,
        open=False,
        run_args=(),
    )

    assert _run_live(args, tmp_path) == 0
    replay_log = cast("Path", captured["replay_log"])
    runtime_dir = tmp_path / "clients" / "web" / ".vibesys-demo"
    assert replay_log == runtime_dir / "run-events.jsonl"
    assert replay_log.read_text() == replay_source.read_text()
    assert captured["instance"] == runtime_dir / "web-gateway.json"
    assert captured["project"] is None
    assert captured["task"] is None


def test_run_live_demo_rejects_operator_run_arguments(tmp_path: Path) -> None:
    args = argparse.Namespace(
        project=None,
        task="spsc",
        port=8765,
        instance=None,
        ssh_target=None,
        browser_origin=[],
        demo=True,
        no_build=True,
        open=False,
        run_args=(),
    )
    (tmp_path / _DEMO_LOG).parent.mkdir(parents=True)
    (tmp_path / _DEMO_LOG).write_text("{}\n")

    with pytest.raises(SystemExit, match="does not accept run arguments"):
        _run_live(args, tmp_path)


def test_local_url_preserves_capability_token_and_port() -> None:
    local, remote_port = _local_url(
        "http://127.0.0.1:8765/?token=secret-token",
        8765,
    )

    assert remote_port == 8765
    assert local == "http://127.0.0.1:8765/?token=secret-token"


def test_local_url_rejects_missing_token() -> None:
    with pytest.raises(SystemExit, match="missing its capability token"):
        _local_url("http://127.0.0.1:8765/", 8765)


def test_local_url_rejects_non_loopback_capability_urls() -> None:
    with pytest.raises(SystemExit, match=r"127\.0\.0\.1 capability URL"):
        _local_url("https://gateway.example:8765/?token=secret", 8765)


@pytest.mark.parametrize("origin", ["ftp://127.0.0.1:5173", "http://127.0.0.1:5173/app"])
def test_browser_url_rejects_non_origins(origin: str) -> None:
    with pytest.raises(SystemExit, match="without a path"):
        _browser_url(origin, "http://127.0.0.1:8765/?token=secret")


def test_browser_url_targets_the_replay_dev_server() -> None:
    assert (
        _browser_url(
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8765/?token=secret",
        )
        == "http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret"
    )


def test_tunnel_requires_a_port_and_matching_forward() -> None:
    missing_port = argparse.Namespace(
        url="http://127.0.0.1/?token=secret",
        local_port=None,
        browser_origin="http://127.0.0.1:5173",
        host="user@host",
    )
    with pytest.raises(SystemExit, match="missing its port"):
        _run_tunnel(missing_port)

    mismatched = argparse.Namespace(
        url="http://127.0.0.1:8765/?token=secret",
        local_port=5173,
        browser_origin="http://127.0.0.1:5173",
        host="user@host",
    )
    with pytest.raises(SystemExit, match="ports must match"):
        _run_tunnel(mismatched)


def test_tunnel_runs_ssh_with_the_capability_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess([], 3)

    # test-isolation: SSH invocation is the behavior under test.
    monkeypatch.setattr(web, "_ssh", lambda: "ssh")
    # test-isolation: SSH invocation is the behavior under test.
    monkeypatch.setattr(web.subprocess, "run", fake_run)
    args = argparse.Namespace(
        url="http://127.0.0.1:8765/?token=secret",
        local_port=None,
        browser_origin="http://127.0.0.1:5173",
        host="user@host",
    )

    assert _run_tunnel(args) == 3
    assert calls[0][0][0] == ["ssh", "-N", "-L", "8765:127.0.0.1:8765", "user@host"]
    assert "Open locally:" in capsys.readouterr().out


def test_status_and_stop_report_gateway_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    instance = tmp_path / "record.json"
    record = _record()

    def discover(path: Path, *, cleanup_stale: bool) -> web.WebInstanceRecord:
        assert path == instance
        assert not cleanup_stale
        return record

    # test-isolation: lifecycle status is isolated from a real gateway process.
    monkeypatch.setattr(web.WebInstanceRecord, "discover", discover)
    assert _run_status(argparse.Namespace(instance=instance)) == 0
    assert "VibeSys web UI:" in capsys.readouterr().out

    # test-isolation: lifecycle stop must not signal the test process.
    monkeypatch.setattr(web.os, "kill", lambda _pid, _sig: None)
    assert _run_stop(argparse.Namespace(instance=instance)) == 0
    assert "Stopped VibeSys web gateway" in capsys.readouterr().out


def test_status_and_stop_are_idempotent_when_record_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def discover(path: Path, *, cleanup_stale: bool) -> None:
        assert path
        assert not cleanup_stale

    # test-isolation: lifecycle status is isolated from a real gateway process.
    monkeypatch.setattr(web.WebInstanceRecord, "discover", discover)
    args = argparse.Namespace(instance=tmp_path / "missing.json")
    assert _run_status(args) == 1
    assert _run_stop(args) == 0


def test_main_dispatches_each_web_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def dispatch(*_args: object, **_kwargs: object) -> int:
        calls.append("called")
        return 0

    # test-isolation: dispatch coverage must not launch any external workflow.
    monkeypatch.setattr(web, "_run_dev", dispatch)
    # test-isolation: dispatch coverage must not launch any external workflow.
    monkeypatch.setattr(web, "_run_live", dispatch)
    # test-isolation: dispatch coverage must not launch any external workflow.
    monkeypatch.setattr(web, "_run_tunnel", dispatch)
    # test-isolation: dispatch coverage must not launch any external workflow.
    monkeypatch.setattr(web, "_run_status", dispatch)
    # test-isolation: dispatch coverage must not launch any external workflow.
    monkeypatch.setattr(web, "_run_stop", dispatch)

    commands = [
        ["dev"],
        ["live", "--project", "project"],
        ["tunnel", "--host", "user@host", "--url", "http://127.0.0.1:8765/?token=secret"],
        ["status", "--instance", "record.json"],
        ["stop", "--instance", "record.json"],
    ]
    for command in commands:
        assert web.main(command) == 0
    assert len(calls) == len(commands)
