"""Tests for the web UI developer and operator helpers."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from entrypoints.web import (
    _browser_url,
    _demo_project,
    _live_command,
    _local_url,
    _parser,
    _port,
    _run_tunnel,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_live_demo_command_uses_the_shared_server_entrypoint(tmp_path: Path) -> None:
    command = _live_command(
        project=tmp_path / "project",
        task="spsc",
        port=8765,
        instance=tmp_path / "web-gateway.json",
        demo=True,
        run_args=(),
        browser_origins=(),
    )

    assert command[-8:] == [
        "--stub-agent",
        "--local",
        "--run-environment",
        "local",
        "--outer-loop",
        "agent",
        "--max-rounds",
        "1",
    ]
    assert command[1:4] == ["-m", "entrypoints.server", "--web"]


def test_live_command_preserves_arguments_after_separator(tmp_path: Path) -> None:
    command = _live_command(
        project=tmp_path / "project",
        task=None,
        port=8765,
        instance=tmp_path / "web-gateway.json",
        demo=False,
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


def test_demo_project_copies_source_without_git_metadata(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / ".git").mkdir(parents=True)
    (source / ".git" / "private").write_text("ignored", encoding="utf-8")
    (source / "README").write_text("demo", encoding="utf-8")

    copied = _demo_project(source)

    assert copied != source
    assert (copied / "README").read_text(encoding="utf-8") == "demo"
    assert not (copied / ".git").exists()


def test_demo_project_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="demo project does not exist"):
        _demo_project(tmp_path / "missing")


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
