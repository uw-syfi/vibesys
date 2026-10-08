"""Tests for fail-closed, port-keyed web gateway discovery."""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis.strategies import integers

from server.runtime import WebPortInspector, WebPortState

if TYPE_CHECKING:
    from pathlib import Path

_PORT = 8765
_PID = 4321
_INODE = 98765


def _write_tcp_table(process_table: Path, *listeners: tuple[int, int]) -> None:
    net = process_table / "net"
    net.mkdir(parents=True, exist_ok=True)
    header = "sl local_address rem_address st tx_queue tr tm->when retrnsmt uid timeout inode"
    rows = [
        f"{index}: 0100007F:{port:04X} 00000000:0000 0A 00000000:00000000 "
        f"00:00000000 00000000 {os.geteuid()} 0 {inode}"
        for index, (port, inode) in enumerate(listeners)
    ]
    (net / "tcp").write_text("\n".join((header, *rows, "")), encoding="ascii")


def _write_mapped_tcp6_listener(process_table: Path, port: int, inode: int) -> None:
    net = process_table / "net"
    net.mkdir(parents=True, exist_ok=True)
    header = "sl local_address rem_address st tx_queue tr tm->when retrnsmt uid timeout inode"
    mapped_loopback = "0000000000000000FFFF00000100007F"
    row = (
        f"0: {mapped_loopback}:{port:04X} {'0' * 32}:0000 0A 00000000:00000000 "
        f"00:00000000 00000000 {os.geteuid()} 0 {inode}"
    )
    (net / "tcp6").write_text(f"{header}\n{row}\n", encoding="ascii")


def _write_process(
    process_table: Path,
    instance: Path,
    *,
    pid: int = _PID,
    gateway: bool = True,
    holds_claim: bool = True,
) -> None:
    process = process_table / str(pid)
    descriptors = process / "fd"
    descriptors.mkdir(parents=True)
    project = instance.parent.parent
    project.mkdir(parents=True, exist_ok=True)
    (process / "cwd").symlink_to(project, target_is_directory=True)
    arguments = (
        (
            "python",
            "-m",
            "entrypoints.server",
            "--web",
            "--web-port",
            str(_PORT),
            "--web-instance",
            str(instance),
        )
        if gateway
        else ("python", "-m", "http.server", str(_PORT))
    )
    (process / "cmdline").write_bytes(b"\0".join(value.encode() for value in arguments) + b"\0")
    # Field 22 is index 19 after the parenthesized process name.
    (process / "stat").write_text(
        f"{pid} (python worker) S {' '.join('0' for _ in range(18))} 123456\n",
        encoding="ascii",
    )
    (descriptors / "8").symlink_to(f"socket:[{_INODE}]")
    claim = instance.with_name(f"{instance.name}.lock")
    claim.parent.mkdir(parents=True, exist_ok=True)
    claim.touch()
    if holds_claim:
        (descriptors / "9").symlink_to(claim)


def test_inspector_identifies_a_gateway_even_when_its_record_is_missing(tmp_path: Path) -> None:
    process_table = tmp_path / "proc"
    instance = tmp_path / "project" / ".vibesys" / "web-gateway.json"
    _write_tcp_table(process_table, (_PORT, _INODE))
    _write_process(process_table, instance)

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.VIBESYS_GATEWAY
    assert observation.holder_pids == (_PID,)
    assert observation.gateway is not None
    assert observation.gateway.instance_path == instance
    assert observation.gateway.socket_inodes == (_INODE,)
    assert not instance.exists()


@pytest.mark.parametrize("missing_identity", ["command", "claim"])
def test_inspector_never_identifies_a_process_without_both_command_and_claim(
    tmp_path: Path,
    missing_identity: str,
) -> None:
    process_table = tmp_path / "proc"
    instance = tmp_path / "project" / ".vibesys" / "web-gateway.json"
    _write_tcp_table(process_table, (_PORT, _INODE))
    _write_process(
        process_table,
        instance,
        gateway=missing_identity != "command",
        holds_claim=missing_identity != "claim",
    )

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.OTHER
    assert observation.gateway is None
    assert observation.holder_pids == (_PID,)


def test_inspector_reports_an_unmappable_listener_as_unknown(tmp_path: Path) -> None:
    process_table = tmp_path / "proc"
    _write_tcp_table(process_table, (_PORT, _INODE))

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.UNKNOWN
    assert observation.gateway is None
    assert observation.holder_pids == ()


def test_inspector_classifies_an_ipv4_mapped_ipv6_loopback_listener(tmp_path: Path) -> None:
    process_table = tmp_path / "proc"
    instance = tmp_path / "project" / ".vibesys" / "web-gateway.json"
    _write_tcp_table(process_table)
    _write_mapped_tcp6_listener(process_table, _PORT, _INODE)
    _write_process(process_table, instance, gateway=False)

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.OTHER
    assert observation.gateway is None
    assert observation.holder_pids == (_PID,)


def test_inspector_refuses_an_ambiguous_listener_shared_by_two_processes(tmp_path: Path) -> None:
    process_table = tmp_path / "proc"
    _write_tcp_table(process_table, (_PORT, _INODE))
    _write_process(
        process_table,
        tmp_path / "first" / ".vibesys" / "web-gateway.json",
    )
    _write_process(
        process_table,
        tmp_path / "second" / ".vibesys" / "web-gateway.json",
        pid=_PID + 1,
    )

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.UNKNOWN
    assert observation.gateway is None
    assert observation.holder_pids == (_PID, _PID + 1)


@given(other_port=integers(min_value=1, max_value=65_535).filter(lambda port: port != _PORT))
def test_inspector_selects_only_the_requested_listening_port(
    tmp_path_factory: pytest.TempPathFactory,
    other_port: int,
) -> None:
    tmp_path = tmp_path_factory.mktemp("port-selection")
    process_table = tmp_path / "proc"
    _write_tcp_table(process_table, (other_port, _INODE))

    observation = WebPortInspector(process_table).inspect(_PORT)

    assert observation.state is WebPortState.FREE


def test_inspector_fails_closed_without_a_linux_process_table(tmp_path: Path) -> None:
    observation = WebPortInspector(tmp_path / "missing-proc").inspect(_PORT)

    assert observation.state is WebPortState.UNKNOWN


def test_inspector_maps_a_real_same_user_non_gateway_listener() -> None:
    with socket.create_server(("127.0.0.1", 0)) as server:
        port = server.getsockname()[1]
        observation = WebPortInspector().inspect(port)

    assert observation.state is WebPortState.OTHER
    assert observation.gateway is None
    assert os.getpid() in observation.holder_pids
