"""Tests for the web UI developer and operator helpers."""

from __future__ import annotations

import pytest

from entrypoints.web import _browser_url, _live_command, _local_url


def test_live_demo_command_uses_the_shared_server_entrypoint(tmp_path) -> None:  # noqa: ANN001
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


def test_live_command_preserves_arguments_after_separator(tmp_path) -> None:  # noqa: ANN001
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


def test_browser_url_targets_the_replay_dev_server() -> None:
    assert (
        _browser_url(
            "http://127.0.0.1:5173",
            "http://127.0.0.1:8765/?token=secret",
        )
        == "http://127.0.0.1:5173/?gateway=http%3A%2F%2F127.0.0.1%3A8765%2F%3Ftoken%3Dsecret"
    )
