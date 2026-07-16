from __future__ import annotations

import hashlib
import importlib.util
import io
import os
import sys
import tarfile
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_KV_STORE = _ROOT / "examples" / "kv-store"
if str(_KV_STORE) not in sys.path:
    sys.path.insert(0, str(_KV_STORE))

from evaluator_support import (  # noqa: E402
    lifecycle,
    procfs_cpu,
    validity,
    ycsb,
)


def _load_benchmark():
    path = _KV_STORE / "benchmark" / "benchmark.py"
    spec = importlib.util.spec_from_file_location("kv_store_benchmark", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_proc_stat(
    proc_root: Path,
    pid: int,
    *,
    parent: int,
    group: int,
    starttime: int,
    user_ticks: int,
    system_ticks: int,
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    fields = [
        "S",
        str(parent),
        str(group),
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(user_ticks),
        str(system_ticks),
        "0",
        "0",
        "0",
        "0",
        "0",
        "0",
        str(starttime),
    ]
    (process / "stat").write_text(f"{pid} (worker with parens) {' '.join(fields)}\n")


def _valid_round() -> dict:
    return {
        "cpu_valid": True,
        "lat": {
            "READ": {"p99": 900.0},
            "UPDATE": {"p99": 800.0},
        },
    }


def _ycsb_archive() -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        content = b"#!/bin/sh\n"
        member = tarfile.TarInfo("ycsb-redis-binding-0.17.0/bin/ycsb.sh")
        member.mode = 0o755
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))
    return data.getvalue()


def test_process_group_snapshot_aggregates_all_processes(tmp_path):
    _write_proc_stat(tmp_path, 10, parent=1, group=10, starttime=100, user_ticks=10, system_ticks=2)
    _write_proc_stat(
        tmp_path, 11, parent=10, group=10, starttime=101, user_ticks=20, system_ticks=3
    )
    _write_proc_stat(tmp_path, 12, parent=1, group=12, starttime=102, user_ticks=99, system_ticks=1)

    assert procfs_cpu.server_processes(6380, process_group=10, proc_root=tmp_path) == {10, 11}
    assert procfs_cpu.cpu_snapshot(6380, process_group=10, proc_root=tmp_path) == {
        (10, 100): 12,
        (11, 101): 23,
    }


def test_listener_discovery_handles_shared_socket_and_descendant(tmp_path):
    (tmp_path / "net").mkdir()
    (tmp_path / "net" / "tcp").write_text(
        "header\n0: 0100007F:18EC 00000000:0000 0A 0 0 0 0 0 12345\n"
    )
    (tmp_path / "net" / "tcp6").write_text("header\n")
    for pid in (20, 21):
        _write_proc_stat(
            tmp_path,
            pid,
            parent=1 if pid == 20 else 20,
            group=20,
            starttime=pid,
            user_ticks=1,
            system_ticks=1,
        )
        (tmp_path / str(pid) / "fd").mkdir()
    os.symlink("socket:[12345]", tmp_path / "20" / "fd" / "3")

    assert procfs_cpu.listener_pids(6380, tmp_path) == {20}
    assert procfs_cpu.server_processes(6380, proc_root=tmp_path) == {20, 21}


def test_cpu_delta_rejects_pid_reuse_or_membership_change():
    assert procfs_cpu.cpu_delta_seconds({(1, 10): 4}, {(1, 11): 8}) is None
    assert procfs_cpu.cpu_delta_seconds({(1, 10): 4}, {(1, 10): 4}) is None


def test_candidate_lifecycle_launches_and_reaps_process_group(tmp_path):
    launcher = tmp_path / "run.sh"
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import socket, sys\n"
        "sock = socket.socket()\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('127.0.0.1', int(sys.argv[1])))\n"
        "sock.listen()\n"
        "while True:\n"
        "    conn, _ = sock.accept()\n"
        "    conn.close()\n"
    )
    launcher.chmod(0o755)

    with lifecycle.candidate_server(workspace=tmp_path) as candidate:
        assert candidate.pid is not None
        assert candidate.process_group is not None
        os.kill(candidate.pid, 0)
        pid = candidate.pid

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_external_port_yields_resolved_target_without_ownership():
    with lifecycle.candidate_server(workspace=Path("/tmp"), port=6380) as candidate:
        assert candidate.port == 6380
        assert candidate.process_group is None
        assert candidate.pid is None


def test_validity_threshold_boundaries():
    checks, reasons = validity.evaluate_validity(
        throughput=10000.0,
        cpu_per_op=10.0,
        rounds=[_valid_round()],
        read_p99_ms=0.999,
        update_p99_ms=0.5,
        saturation_gain_pct=10.0,
        min_throughput=10000.0,
        max_read_p99_ms=1.0,
        max_update_p99_ms=1.0,
        max_saturation_gain_pct=10.0,
    )
    assert all(checks.values())
    assert reasons == []


@pytest.mark.parametrize(
    ("overrides", "failed_check"),
    [
        ({"throughput": 9999.9}, "throughput_floor"),
        ({"read_p99_ms": 1.0}, "read_p99"),
        ({"update_p99_ms": None}, "update_p99"),
        ({"cpu_per_op": float("nan")}, "score_available"),
        ({"saturation_gain_pct": 10.1}, "saturation"),
        ({"saturation_gain_pct": -10.1}, "saturation"),
        ({"rounds": [{**_valid_round(), "cpu_valid": False}]}, "cpu_samples"),
    ],
)
def test_validity_rejects_each_invalid_gate(overrides, failed_check):
    arguments = {
        "throughput": 10000.0,
        "cpu_per_op": 10.0,
        "rounds": [_valid_round()],
        "read_p99_ms": 0.9,
        "update_p99_ms": 0.8,
        "saturation_gain_pct": 10.0,
        "min_throughput": 10000.0,
        "max_read_p99_ms": 1.0,
        "max_update_p99_ms": 1.0,
        "max_saturation_gain_pct": 10.0,
    }
    arguments.update(overrides)
    checks, reasons = validity.evaluate_validity(**arguments)
    assert checks[failed_check] is False
    assert reasons


def test_worst_latency_uses_all_rounds():
    benchmark = _load_benchmark()
    rounds = [
        {"lat": {"READ": {"p99": 500.0}}},
        {"lat": {"READ": {"p99": 1200.0}}},
    ]
    assert benchmark._worst_latency_ms(rounds, "READ") == 1.2


def test_safe_extract_rejects_traversal(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("../escape")
        member.size = 1
        tar.addfile(member, io.BytesIO(b"x"))
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        with pytest.raises(ValueError, match="unsafe YCSB archive path"):
            ycsb.safe_extract(tar, tmp_path)


def test_safe_extract_rejects_links(tmp_path):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as tar:
        member = tarfile.TarInfo("link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        tar.addfile(member)
    archive.seek(0)
    with tarfile.open(fileobj=archive, mode="r:gz") as tar:
        with pytest.raises(ValueError, match="unsupported YCSB archive member"):
            ycsb.safe_extract(tar, tmp_path)


def test_ycsb_install_is_verified_atomic_and_reused(tmp_path, monkeypatch):
    archive = _ycsb_archive()
    cache = tmp_path / ".cache"
    home = cache / "ycsb-redis-binding-0.17.0"
    sha = hashlib.sha256(archive).hexdigest()
    monkeypatch.setattr(ycsb.urllib.request, "urlopen", lambda _: io.BytesIO(archive))
    home.mkdir(parents=True)
    (home / "partial").write_text("incomplete")

    ycsb.ensure_ycsb(cache=cache, home=home, sha256=sha)

    assert (home / "bin" / "ycsb.sh").is_file()
    assert not (home / "partial").exists()
    monkeypatch.setattr(
        ycsb.urllib.request,
        "urlopen",
        lambda _: pytest.fail("verified cache should be reused"),
    )
    ycsb.ensure_ycsb(cache=cache, home=home, sha256=sha)


def test_ycsb_checksum_failure_does_not_install(tmp_path, monkeypatch):
    archive = _ycsb_archive()
    cache = tmp_path / ".cache"
    home = cache / "ycsb-redis-binding-0.17.0"
    monkeypatch.setattr(ycsb.urllib.request, "urlopen", lambda _: io.BytesIO(archive))

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        ycsb.ensure_ycsb(cache=cache, home=home, sha256="0" * 64)
    assert not home.exists()
