"""Contract tests for agent GPU commands run as local Slurm jobs."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import (
    GpuCommand,
    GpuJobRequest,
    SlurmGpuConfig,
    SlurmGpuConfigError,
    SlurmGpuLauncher,
    SlurmGpuRequestError,
    choose_partition,
    load_slurm_gpu_config,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
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
        status = SlurmGpuLauncher(_config(tmp_path)).run(
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
        SlurmGpuLauncher(_config(tmp_path)).run(
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
