"""Contract tests for agent GPU commands run as local Slurm jobs."""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import sys
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import (
    GPU_BROKER_SOCKET_ENV,
    GPU_BROKER_TOKEN_ENV,
    GpuCommand,
    GpuJobRequest,
    SlurmGpuBroker,
    SlurmGpuConfig,
    SlurmGpuConfigError,
    SlurmGpuLauncher,
    SlurmGpuRequestError,
    choose_partition,
    load_slurm_gpu_config,
)
from vs_sandbox.api.slurm import run_brokered_gpu_command as run_brokered

# test-isolation: main is the CLI entry point and is intentionally absent from the library API.
from vs_sandbox.slurm_gpu_client import main as client_main

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from pathlib import Path

# Recorded `slurm-windows --json` output: 1-4 GPUs start now on main for up
# to 57 minutes; 5-8 GPUs wait on both main and priority.
WINDOWS: Mapping[str, object] = {
    "now": "2026-10-08T20:33:42",
    "partitions": [
        {
            "partition": "main",
            "partition_max_time": "02:00:00",
            "groups": [
                {
                    "gpus_min": 1,
                    "gpus_max": 4,
                    "options": [
                        {"max_time": "00:57:00", "start": "now"},
                        {"max_time": "02:00:00", "start": "2026-10-08T23:31"},
                    ],
                },
                {
                    "gpus_min": 5,
                    "gpus_max": 8,
                    "options": [{"max_time": "02:00:00", "start": "2026-10-08T23:31"}],
                },
            ],
        },
        {
            "partition": "priority",
            "partition_max_time": "00:30:00",
            "groups": [
                {
                    "gpus_min": 1,
                    "gpus_max": 4,
                    "options": [{"max_time": "00:30:00", "start": "now"}],
                },
                {
                    "gpus_min": 5,
                    "gpus_max": 8,
                    "options": [{"max_time": "00:30:00", "start": "2026-10-08T21:31"}],
                },
            ],
        },
    ],
}

# Stands in for srun and scancel: records its argv, then runs what follows `--`.
FAKE_SLURM = """\
import os, sys
with open(os.environ["FAKE_SLURM_LOG"], "a") as log:
    log.write(repr(sys.argv[1:]) + "\\n")
if "--" in sys.argv:
    command = sys.argv[sys.argv.index("--") + 1 :]
    os.execvp(command[0], command)
"""


def _config(tmp_path: Path, **updates: object) -> SlurmGpuConfig:
    fake = tmp_path / "fake_slurm.py"
    fake.write_text(FAKE_SLURM)
    values: dict[str, object] = {
        "partitions": ("main", "priority"),
        "max_gpus": 8,
        "max_time_minutes": 120,
        "srun_command": (sys.executable, str(fake)),
        "scancel_command": (sys.executable, str(fake)),
    }
    values.update(updates)
    return SlurmGpuConfig.model_validate(values)


@pytest.fixture
def slurm_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "slurm.log"
    monkeypatch.setenv("FAKE_SLURM_LOG", str(log))
    return log


def _no_windows(_config: SlurmGpuConfig) -> None:
    return None


class TestConfig:
    def test_loads_the_operator_table(self, tmp_path: Path) -> None:
        path = tmp_path / "slurm-gpu.toml"
        path.write_text(
            '[slurm_gpu]\npartitions = ["main", "priority"]\nmax_gpus = 8\n'
            'max_time_minutes = 120\nwindows_command = ["slurm-windows", "--json"]\n'
        )
        config = load_slurm_gpu_config(path)
        assert config.partitions == ("main", "priority")
        assert config.windows_command == ("slurm-windows", "--json")

    def test_rejects_unknown_settings_by_name(self, tmp_path: Path) -> None:
        path = tmp_path / "slurm-gpu.toml"
        path.write_text(
            '[slurm_gpu]\npartitions = ["main"]\nmax_gpus = 8\nmax_time_minutes = 60\n'
            'account = "secret-account"\n'
        )
        with pytest.raises(SlurmGpuConfigError, match=r"slurm_gpu\.account") as error:
            load_slurm_gpu_config(path)
        assert "secret-account" not in str(error.value)

    def test_requests_above_the_limits_are_rejected_not_clamped(self, tmp_path: Path) -> None:
        config = _config(tmp_path, max_gpus=4, max_time_minutes=30, gate_time_minutes=30)
        assert config.request(None, None) == GpuJobRequest(gpus=1, time_minutes=30)
        with pytest.raises(SlurmGpuRequestError, match="limit of 4"):
            config.request(8, 10)
        with pytest.raises(SlurmGpuRequestError, match="limit of 30 minutes"):
            config.request(1, 31)


class TestPartition:
    @pytest.mark.parametrize(
        ("gpus", "minutes", "expected"),
        [
            (4, 50, "main"),  # main starts it now
            (8, 25, "main"),  # nothing starts now; main is preferred
            (2, 90, "main"),  # only main's limit fits
        ],
    )
    def test_prefers_a_partition_that_starts_now(
        self, tmp_path: Path, gpus: int, minutes: int, expected: str
    ) -> None:
        request = GpuJobRequest(gpus=gpus, time_minutes=minutes)
        assert choose_partition(_config(tmp_path), request, WINDOWS) == expected

    def test_uses_priority_for_a_short_job_main_cannot_start(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        request = GpuJobRequest(gpus=2, time_minutes=20)
        busy = json.loads(json.dumps(WINDOWS))
        busy["partitions"][0]["groups"][0]["options"][0]["start"] = "2026-10-08T23:31"
        assert choose_partition(config, request, busy) == "priority"

    def test_never_picks_a_partition_whose_limit_is_too_short(self, tmp_path: Path) -> None:
        config = _config(tmp_path, partitions=("priority", "main"))
        request = GpuJobRequest(gpus=8, time_minutes=60)
        assert choose_partition(config, request, WINDOWS) == "main"

    @pytest.mark.parametrize("windows", [None, {"partitions": "garbled"}, {}])
    def test_without_a_usable_report_uses_the_first_partition(
        self, tmp_path: Path, windows: Mapping[str, object] | None
    ) -> None:
        request = GpuJobRequest(gpus=1, time_minutes=5)
        assert choose_partition(_config(tmp_path), request, windows) == "main"


class TestLauncher:
    def test_streams_output_and_returns_the_command_status(
        self, tmp_path: Path, slurm_log: Path
    ) -> None:
        output = bytearray()
        status = SlurmGpuLauncher(_config(tmp_path), windows=_no_windows).run(
            GpuJobRequest(gpus=2, time_minutes=7),
            GpuCommand(
                argv=("sh", "-c", "echo from-job; exit 3"), cwd=tmp_path, env=dict(os.environ)
            ),
            write=output.extend,
            cancel=threading.Event(),
        )
        srun_args = slurm_log.read_text().splitlines()[0]

        assert status == 3
        assert b"from-job\n" in output
        for expected in ("--partition=main", "--gres=gpu:2", "--time=7", "--ntasks=1"):
            assert expected in srun_args

    def test_cancel_scancels_the_job_by_its_unique_name(
        self, tmp_path: Path, slurm_log: Path
    ) -> None:
        cancel = threading.Event()
        cancel.set()
        output = bytearray()
        SlurmGpuLauncher(_config(tmp_path), windows=_no_windows).run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("sleep", "60"), cwd=tmp_path, env=dict(os.environ)),
            write=output.extend,
            cancel=cancel,
        )
        # The launcher announces the job name before it starts srun, and cancel is
        # synchronous, so both facts are settled when run returns. srun may be
        # stopped before it logs anything, so the log's line order is not asserted.
        match = re.search(r"job-name=(\S+)", output.decode())
        assert match is not None
        job_name = match.group(1)

        scancel_lines = [
            line for line in slurm_log.read_text().splitlines() if f"--name={job_name}" in line
        ]
        assert len(scancel_lines) == 1
        assert "--me" in scancel_lines[0]


class _RecordingLauncher:
    """Fake launcher: records the confined command and waits for cancellation."""

    def __init__(self, *, block: bool = False) -> None:
        self.commands: list[GpuCommand] = []
        self.requests: list[GpuJobRequest] = []
        self.cancelled = threading.Event()
        self._block = block

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: threading.Event,
    ) -> int:
        self.requests.append(request)
        self.commands.append(command)
        write(b"started\n")
        if self._block and cancel.wait(10):
            self.cancelled.set()
        return 5


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "sub").mkdir(parents=True)
    return root


@contextmanager
def _serving(
    tmp_path: Path, workspace: Path, launcher: _RecordingLauncher
) -> Iterator[SlurmGpuBroker]:
    broker = SlurmGpuBroker(
        _config(tmp_path),
        tmp_path / "gpu.sock",
        workspaces=(workspace,),
        worktree_roots=(tmp_path / "worktrees",),
        wrap=lambda root, argv: ["confine", str(root), *argv],
        launcher=launcher,
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


class TestBroker:
    def test_confines_the_command_and_relays_output_and_status(
        self,
        tmp_path: Path,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        launcher = _RecordingLauncher()
        monkeypatch.chdir(workspace / "sub")
        monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "")
        monkeypatch.setenv("KEEP_ME", "1")
        with _serving(tmp_path, workspace, launcher) as broker:
            _point_at(monkeypatch, broker)
            status = run_brokered(["nvidia-smi"], gpus=4, time_minutes=9)

        command = launcher.commands[0]
        assert status == 5
        assert capsysbinary.readouterr().out == b"started\n"
        assert launcher.requests == [GpuJobRequest(gpus=4, time_minutes=9)]
        assert command.argv[:2] == ("confine", str(workspace.resolve()))
        assert command.argv[-2:] == (str((workspace / "sub").resolve()), "nvidia-smi")
        assert command.env["KEEP_ME"] == "1"
        assert "CUDA_VISIBLE_DEVICES" not in command.env

    def test_a_candidate_worktree_is_confined_to_itself(
        self, tmp_path: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        candidate = tmp_path / "worktrees" / "candidate-1"
        (candidate / "src").mkdir(parents=True)
        launcher = _RecordingLauncher()
        monkeypatch.chdir(candidate / "src")
        with _serving(tmp_path, workspace, launcher) as broker:
            _point_at(monkeypatch, broker)
            run_brokered(["true"], gpus=1, time_minutes=1)

        assert launcher.commands[0].argv[1] == str(candidate.resolve())

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"token": "wrong"}, "invalid GPU broker capability"),
            ({"cwd": "/"}, "inside the run's workspace"),
            ({"gpus": 9}, "operator limit of 8"),
        ],
    )
    def test_rejects_requests_outside_the_capability(
        self,
        tmp_path: Path,
        workspace: Path,
        overrides: Mapping[str, object],
        message: str,
    ) -> None:
        launcher = _RecordingLauncher()
        with _serving(tmp_path, workspace, launcher) as broker:
            reply = _raw_request(
                broker,
                {
                    "token": broker.token,
                    "argv": ["true"],
                    "cwd": str(workspace),
                    "gpus": 1,
                    "time_minutes": 1,
                    "env": {},
                    **overrides,
                },
            )

        assert message in str(reply["error"])
        assert launcher.commands == []

    def test_closing_the_connection_cancels_the_job(self, tmp_path: Path, workspace: Path) -> None:
        launcher = _RecordingLauncher(block=True)
        with _serving(tmp_path, workspace, launcher) as broker:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(broker.socket_path))
                client.sendall(_frame(broker, workspace))
                with client.makefile("rb") as frames:
                    first = json.loads(frames.readline())
            cancelled = launcher.cancelled.wait(10)

        assert base64.b64decode(first["output"]) == b"started\n"
        assert cancelled


def _point_at(monkeypatch: pytest.MonkeyPatch, broker: SlurmGpuBroker) -> None:
    monkeypatch.setenv(GPU_BROKER_SOCKET_ENV, str(broker.socket_path))
    monkeypatch.setenv(GPU_BROKER_TOKEN_ENV, broker.token)


def _frame(broker: SlurmGpuBroker, workspace: Path) -> bytes:
    request = {
        "token": broker.token,
        "argv": ["true"],
        "cwd": str(workspace),
        "gpus": 1,
        "time_minutes": 1,
        "env": {},
    }
    return json.dumps(request).encode() + b"\n"


def _raw_request(broker: SlurmGpuBroker, request: Mapping[str, object]) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(broker.socket_path))
        client.sendall(json.dumps(request).encode() + b"\n")
        with client.makefile("rb") as frames:
            return json.loads(frames.readline())


class TestClient:
    def test_without_a_broker_or_config_it_refuses(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv(GPU_BROKER_SOCKET_ENV, raising=False)
        monkeypatch.delenv(GPU_BROKER_TOKEN_ENV, raising=False)
        assert client_main(["--", "nvidia-smi"]) == 2
        assert "no GPU broker" in capsys.readouterr().err

    def test_on_the_host_it_runs_srun_directly(
        self,
        tmp_path: Path,
        slurm_log: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsysbinary: pytest.CaptureFixture[bytes],
    ) -> None:
        fake = tmp_path / "fake_slurm.py"
        fake.write_text(FAKE_SLURM)
        config = tmp_path / "slurm-gpu.toml"
        config.write_text(
            '[slurm_gpu]\npartitions = ["main"]\nmax_gpus = 8\nmax_time_minutes = 60\n'
            f'srun_command = ["{sys.executable}", "{fake}"]\n'
        )
        monkeypatch.delenv(GPU_BROKER_SOCKET_ENV, raising=False)
        monkeypatch.chdir(tmp_path)
        status = client_main(
            ["--config", str(config), "--gpus", "8", "--time", "40", "--", "sh", "-c", "exit 7"]
        )

        assert status == 7
        assert "--gres=gpu:8" in slurm_log.read_text()
        assert b"partition=main" in capsysbinary.readouterr().out
