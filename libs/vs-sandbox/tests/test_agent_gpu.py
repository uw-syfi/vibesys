"""Contract tests for agent GPU commands run as local Slurm jobs."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    AgentGpuLauncher,
    AgentGpuRequestError,
    GpuCommand,
    GpuJobRequest,
    choose_partition,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ScriptedPipedProcess,
    ScriptedProbe,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

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


def _config(**updates: object) -> AgentGpuConfig:
    values: dict[str, object] = {
        "partitions": ("main", "priority"),
        "max_gpus": 8,
        "max_time_minutes": 120,
    }
    values.update(updates)
    return AgentGpuConfig.model_validate(values)


def _simulated(
    threads: SimThreads, process: ScriptedPipedProcess
) -> tuple[AgentGpuLauncher, FakePipedProcessLauncher, ScriptedProbe]:
    """A launcher whose ``srun`` is *process* and whose ``scancel`` and ``squeue`` succeed."""
    processes = FakePipedProcessLauncher(lambda _start: process)
    probe = ScriptedProbe(lambda _argv: ProbeResult(0, ""))
    launcher = AgentGpuLauncher(
        _config(), ("slurm-shell",), probe=probe, processes=processes, threads=threads
    )
    return launcher, processes, probe


class TestConfig:
    def test_requests_above_the_limits_are_rejected_not_clamped(self) -> None:
        config = _config(max_gpus=4, max_time_minutes=30)
        assert config.request(None, None) == GpuJobRequest(gpus=1, time_minutes=30)
        with pytest.raises(AgentGpuRequestError, match="limit of 4"):
            config.request(8, 10)
        with pytest.raises(AgentGpuRequestError, match="limit of 30 minutes"):
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
        self, gpus: int, minutes: int, expected: str
    ) -> None:
        request = GpuJobRequest(gpus=gpus, time_minutes=minutes)
        assert choose_partition(_config(), request, WINDOWS) == expected

    def test_uses_priority_for_a_short_job_main_cannot_start(self) -> None:
        config = _config()
        request = GpuJobRequest(gpus=2, time_minutes=20)
        busy = json.loads(json.dumps(WINDOWS))
        busy["partitions"][0]["groups"][0]["options"][0]["start"] = "2026-10-08T23:31"
        assert choose_partition(config, request, busy) == "priority"

    def test_never_picks_a_partition_whose_limit_is_too_short(self) -> None:
        config = _config(partitions=("priority", "main"))
        request = GpuJobRequest(gpus=8, time_minutes=60)
        assert choose_partition(config, request, WINDOWS) == "main"

    @pytest.mark.parametrize("windows", [None, {"partitions": "garbled"}, {}])
    def test_without_a_usable_report_uses_the_first_partition(
        self, windows: Mapping[str, object] | None
    ) -> None:
        request = GpuJobRequest(gpus=1, time_minutes=5)
        assert choose_partition(_config(), request, windows) == "main"


class TestLauncher:
    def test_streams_output_and_returns_the_command_status(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.write("from-job\n")
        process.exit(3)
        launcher, processes, _probe = _simulated(threads, process)
        output = bytearray()

        status = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=2, time_minutes=7),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=threads.event(),
            )
        )

        assert status == 3
        assert output.endswith(b"from-job\n")
        (start,) = processes.started
        assert start.argv[0] == "slurm-shell"
        srun_args = start.argv[-1]
        for expected in ("--partition=main", "--gres=gpu:2", "--time=7", "--ntasks=1"):
            assert expected in srun_args

    def test_cancel_scancels_the_job_by_its_unique_name(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        launcher, _processes, probe = _simulated(threads, process)
        cancel = threads.event()
        cancel.set()
        output = bytearray()

        threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=cancel,
            )
        )

        match = re.search(r"job-name=(\S+)", output.decode())
        assert match is not None
        job_name = match.group(1)
        commands = [argv[-1] for argv, _timeout in probe.calls]
        scancels = [line for line in commands if line.startswith("scancel")]
        assert scancels == [f"scancel --name={job_name} --me"]
        assert [line for line in commands if line.startswith("squeue")] == [
            f"squeue --name={job_name} --me --noheader --format=%i"
        ]
        assert process.signals[0] == "terminate"
        assert b"cancel unconfirmed" not in output


_LIMITS = {"partitions": ["main"], "max_gpus": 2, "max_time_minutes": 30}
_path_segment = st.from_regex(r"[A-Za-z0-9_.-]{1,8}", fullmatch=True).filter(
    lambda segment: segment not in {".", ".."}
)


@given(segments=st.lists(_path_segment, min_size=1, max_size=4, unique=True))
def test_read_only_paths_accept_absolute_normalized_paths(segments: list[str]) -> None:
    paths = ["/" + "/".join(segments[: n + 1]) for n in range(len(segments))]

    config = AgentGpuConfig.model_validate({**_LIMITS, "read_only_paths": paths})

    assert config.read_only_paths == tuple(paths)


@pytest.mark.parametrize(
    "path",
    ["", "/", "relative/venv", "~/venv", "/opt/../etc", "/opt/./venv", "/opt//venv", "/opt/venv/"],
)
def test_read_only_paths_reject_what_is_not_absolute_and_normalized(path: str) -> None:
    with pytest.raises(ValueError, match="read_only_paths"):
        AgentGpuConfig.model_validate({**_LIMITS, "read_only_paths": [path]})


def test_read_only_paths_reject_a_repeated_path() -> None:
    with pytest.raises(ValueError, match="repeat"):
        AgentGpuConfig.model_validate({**_LIMITS, "read_only_paths": ["/opt/venv", "/opt/venv"]})


def test_there_is_no_writable_counterpart_to_read_only_paths() -> None:
    with pytest.raises(ValueError, match="writable_paths"):
        AgentGpuConfig.model_validate({**_LIMITS, "writable_paths": ["/opt/venv"]})
