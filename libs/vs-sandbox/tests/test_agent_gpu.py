"""Contract tests for agent GPU commands run as local Slurm jobs."""

from __future__ import annotations

import io
import json
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    AgentGpuLauncher,
    AgentGpuRequestError,
    GpuCommand,
    GpuJobOutcome,
    GpuJobRequest,
    choose_partition,
    parse_partition_limits,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    RefusingThreads,
    ScriptedPipedProcess,
    ScriptedProbe,
    SimThreads,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import Event, Threads, Worker

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
_RELAY_FAILURE = "srun output relay failed"


class _ReadFailurePipe(io.TextIOBase):
    def read(self, size: int | None = -1, /) -> str:
        del size
        raise OSError(_RELAY_FAILURE)


class _ReadFailureProcess(ScriptedPipedProcess):
    """A submitted process whose local output relay fails before it settles."""

    @property
    def stdout(self) -> io.TextIOBase:
        return _ReadFailurePipe()


class _WaitFailureEvent:
    """Cancellation signal whose wait boundary fails inside the watcher."""

    def is_set(self) -> bool:
        return False

    def set(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        raise OSError


class _SetFailureEvent:
    """An event whose bookkeeping fails after the watcher already failed."""

    def __init__(self, delegate: Event) -> None:
        self._delegate = delegate

    def is_set(self) -> bool:
        return self._delegate.is_set()

    def set(self) -> None:
        raise _RecordCancellationError

    def clear(self) -> None:
        self._delegate.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)


class _EventSetFailureThreads:
    """Delegate scheduling while faulting one launcher event and observing its watcher."""

    def __init__(self, delegate: SimThreads, *, failing_event: int) -> None:
        self._delegate = delegate
        self._failing_event = failing_event
        self._events = 0
        self._watcher: Worker | None = None

    def event(self) -> Event:
        self._events += 1
        event = self._delegate.event()
        return _SetFailureEvent(event) if self._events == self._failing_event else event

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._delegate.spawn(target, name=name, daemon=daemon)
        if name == "vibesys-gpu-cancel":
            self._watcher = worker
        return worker

    @property
    def watcher_alive(self) -> bool:
        """Whether the launcher's cancellation watcher still owns a live thread."""
        return self._watcher is not None and self._watcher.is_alive()

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _WatcherJoinError(OSError):
    """Injected loss of the cancellation worker's settlement acknowledgement."""


class _DelayedWatcherError(OSError):
    """Injected failure from an old watcher after a replacement has started."""


class _UnjoinedWatcher:
    """A worker whose owner cannot join or prove that it stopped."""

    def __init__(self, delegate: Worker, fault: str) -> None:
        self._delegate = delegate
        self._fault = fault

    @property
    def name(self) -> str:
        return self._delegate.name

    def is_alive(self) -> bool:
        return True

    def join(self, timeout: float | None = None) -> None:
        del timeout
        if self._fault == "join":
            raise _WatcherJoinError


class _WatcherJoinFailureThreads:
    def __init__(self, delegate: SimThreads, fault: str) -> None:
        self._delegate = delegate
        self._fault = fault

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._delegate.spawn(target, name=name, daemon=daemon)
        return (
            cast("Worker", _UnjoinedWatcher(worker, self._fault))
            if name == "vibesys-gpu-cancel"
            else worker
        )

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _FirstWatcherJoinFailureThreads(_WatcherJoinFailureThreads):
    """Lose ownership proof only for the first stable-name job's watcher."""

    def __init__(self, delegate: SimThreads, fault: str) -> None:
        super().__init__(delegate, fault)
        self._watchers = 0

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._delegate.spawn(target, name=name, daemon=daemon)
        if name != "vibesys-gpu-cancel":
            return worker
        self._watchers += 1
        return (
            cast("Worker", _UnjoinedWatcher(worker, self._fault)) if self._watchers == 1 else worker
        )


class _DelayedWaitFailureEvent:
    """Fail one old watcher only after a replacement job has started."""

    def __init__(self, threads: SimThreads) -> None:
        self.entered = threads.event()
        self.release = threads.event()

    def is_set(self) -> bool:
        return False

    def set(self) -> None:
        pass

    def clear(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> bool:
        del timeout
        self.entered.set()
        assert self.release.wait(100), "the test never released the old cancellation watcher"
        raise _DelayedWatcherError


class _RecordCancellationError(RuntimeError):
    """Injected failure while recording a watcher-requested stop."""


# Recorded `sinfo --noheader --format="%P %l"`: main is the default partition.
SINFO = "main* 2:00:00\npriority 30:00\n"
LIMITS = parse_partition_limits(SINFO)


def _scheduler_answer(argv: tuple[str, ...], *, queued: str = "") -> ProbeResult:
    """Answer partition discovery and optionally report the launched job queued."""
    command = argv[-1]
    if command.startswith("sinfo"):
        return ProbeResult(0, SINFO)
    return ProbeResult(0, queued if "squeue" in command else "")


def _config(**updates: object) -> AgentGpuConfig:
    values: dict[str, object] = {
        "partitions": ("main", "priority"),
        "max_gpus": 8,
        "max_time_minutes": 120,
    }
    values.update(updates)
    return AgentGpuConfig.model_validate(values)


def _simulated(
    threads: Threads, process: ScriptedPipedProcess
) -> tuple[AgentGpuLauncher, FakePipedProcessLauncher, ScriptedProbe]:
    """A launcher whose ``srun`` is *process* and whose ``scancel`` and ``squeue`` succeed."""
    processes = FakePipedProcessLauncher(lambda _start: process)
    probe = ScriptedProbe(
        lambda argv: ProbeResult(0, SINFO if argv[-1].startswith("sinfo") else "")
    )
    launcher = AgentGpuLauncher(
        _config(), ("slurm-shell",), probe=probe, processes=processes, threads=threads
    )
    return launcher, processes, probe


class TestConfig:
    @pytest.mark.parametrize(
        "interval",
        [0.0, -1.0, float("nan"), float("inf"), float("-inf")],
        ids=["zero", "negative", "nan", "inf", "-inf"],
    )
    def test_a_queue_check_interval_that_is_not_a_positive_number_is_rejected_by_name(
        self, interval: float
    ) -> None:
        with pytest.raises(ValueError, match="queue_check_interval_seconds"):
            _config(queue_check_interval_seconds=interval)

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
        assert choose_partition(_config(), request, LIMITS, WINDOWS) == expected

    def test_uses_priority_for_a_short_job_main_cannot_start(self) -> None:
        config = _config()
        request = GpuJobRequest(gpus=2, time_minutes=20)
        busy = json.loads(json.dumps(WINDOWS))
        busy["partitions"][0]["groups"][0]["options"][0]["start"] = "2026-10-08T23:31"
        assert choose_partition(config, request, LIMITS, busy) == "priority"

    def test_never_picks_a_partition_whose_limit_is_too_short(self) -> None:
        config = _config(partitions=("priority", "main"))
        request = GpuJobRequest(gpus=8, time_minutes=60)
        assert choose_partition(config, request, LIMITS, WINDOWS) == "main"

    @pytest.mark.parametrize("minutes", [35, 45])
    def test_without_a_window_report_skips_a_partition_whose_limit_is_too_short(
        self, minutes: int
    ) -> None:
        # Seen live: priority (30 min) listed first, a 35 and a 45 minute request.
        config = _config(partitions=("priority", "main"))
        request = GpuJobRequest(gpus=1, time_minutes=minutes)
        assert choose_partition(config, request, LIMITS, None) == "main"

    @pytest.mark.parametrize("windows", [None, {"partitions": "garbled"}, {}])
    def test_a_missing_or_malformed_report_picks_the_first_partition_that_fits(
        self, windows: Mapping[str, object] | None
    ) -> None:
        request = GpuJobRequest(gpus=1, time_minutes=5)
        assert choose_partition(_config(), request, LIMITS, windows) == "main"

    def test_a_request_no_limit_fits_is_rejected_naming_every_limit(self) -> None:
        config = _config(partitions=("priority", "main", "ghost"))
        with pytest.raises(AgentGpuRequestError) as caught:
            choose_partition(config, GpuJobRequest(gpus=1, time_minutes=121), LIMITS, None)
        message = str(caught.value)
        assert "--time 121" in message
        assert "priority (limit 30 minutes)" in message
        assert "main (limit 120 minutes)" in message
        assert "ghost (limit unknown)" in message

    def test_without_any_limits_a_request_is_rejected_not_sent_to_the_first_partition(self) -> None:
        with pytest.raises(AgentGpuRequestError, match="limit unknown"):
            choose_partition(_config(), GpuJobRequest(gpus=1, time_minutes=1), {}, None)

    @given(
        limits=st.lists(st.integers(1, 500) | st.just(math.inf), min_size=1, max_size=5),
        minutes=st.integers(1, 600),
        windows=st.sampled_from([None, WINDOWS, {"partitions": "garbled"}]),
    )
    def test_chooses_a_fitting_partition_or_rejects(
        self,
        limits: list[float],
        minutes: int,
        windows: Mapping[str, object] | None,
    ) -> None:
        names = tuple(f"p{n}" for n in range(len(limits)))
        config = _config(partitions=names)
        request = GpuJobRequest(gpus=1, time_minutes=minutes)
        by_name = dict(zip(names, limits, strict=True))
        fitting = [name for name in names if by_name[name] >= minutes]
        if not fitting:
            with pytest.raises(AgentGpuRequestError, match=f"--time {minutes} "):
                choose_partition(config, request, by_name, windows)
        else:
            # The window report names main and priority only, so it never prefers p<n>.
            assert choose_partition(config, request, by_name, windows) == fitting[0]


class TestPartitionLimits:
    @pytest.mark.parametrize(
        ("text", "minutes"),
        [
            ("2:00:00", 120),
            ("30:00", 30),
            ("1-00:00:00", 1440),
            ("2-12:00:00", 3600),
            ("0:45", 0.75),
            ("infinite", math.inf),
            ("UNLIMITED", math.inf),
        ],
    )
    def test_parses_slurm_time_limits(self, text: str, minutes: float) -> None:
        assert parse_partition_limits(f"main* {text}\n") == {"main": minutes}

    def test_drops_the_default_marker_and_ignores_malformed_lines(self) -> None:
        text = "main* 2:00:00\nbroken\nodd n/a\nthree 1 2\n\npriority 30:00\n"
        assert parse_partition_limits(text) == {"main": 120, "priority": 30}

    @given(
        days=st.integers(0, 30),
        hours=st.integers(0, 23),
        minutes=st.integers(0, 59),
        seconds=st.integers(0, 59),
    )
    def test_round_trips_formatted_limits(
        self, days: int, hours: int, minutes: int, seconds: int
    ) -> None:
        text = (
            f"{days}-{hours:02d}:{minutes:02d}:{seconds:02d}"
            if days
            else (f"{hours}:{minutes:02d}:{seconds:02d}")
        )
        expected = (days * 24 + hours) * 60 + minutes + seconds / 60
        assert parse_partition_limits(f"p {text}") == {"p": expected}


class TestLauncher:
    def test_streams_output_and_returns_the_command_status(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.write("from-job\n")
        process.exit(3)
        launcher, processes, _probe = _simulated(threads, process)
        output = bytearray()

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=2, time_minutes=7),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=threads.event(),
            )
        )

        assert outcome.exit_code == 3
        assert outcome.termination_confirmed
        assert output.endswith(b"from-job\n")
        (start,) = processes.started
        assert start.argv[0] == "slurm-shell"
        srun_args = start.argv[-1]
        for expected in ("--partition=main", "--gres=gpu:2", "--time=7", "--ntasks=1"):
            assert expected in srun_args

    @pytest.mark.parametrize("minutes", [35, 45])
    def test_a_request_the_first_partition_cannot_hold_goes_to_one_that_can(
        self, minutes: int
    ) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.exit(0)
        processes = FakePipedProcessLauncher(lambda _start: process)
        probe = ScriptedProbe(
            lambda argv: ProbeResult(0, SINFO if argv[-1].startswith("sinfo") else "")
        )
        launcher = AgentGpuLauncher(
            _config(partitions=("priority", "main")),
            ("slurm-shell",),
            probe=probe,
            processes=processes,
            threads=threads,
        )

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=minutes),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=threads.event(),
            )
        )

        assert outcome.exit_code == 0
        assert outcome.termination_confirmed
        assert outcome.failure is None
        (start,) = processes.started
        assert "--partition=main" in start.argv[-1]

    def test_a_request_no_partition_holds_never_reaches_srun(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        launcher, processes, _probe = _simulated(threads, process)
        output = bytearray()

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=121),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=threads.event(),
            )
        )

        assert outcome.exit_code == 2
        assert outcome.termination_confirmed
        assert outcome.failure is None
        assert processes.started == []
        assert b"rejected before srun" in output
        assert b"--time 121" in output

    def test_a_refused_cancellation_watcher_start_settles_and_confirms_the_job(self) -> None:
        simulator = SimThreads()
        threads = RefusingThreads(simulator, {"vibesys-gpu-cancel": 1})
        process = ScriptedPipedProcess(simulator, merge_stderr=True)
        launcher, _processes, probe = _simulated(threads, process)

        outcome = simulator.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=threads.event(),
            )
        )

        assert outcome.termination_confirmed
        assert outcome.failure is not None
        assert process.signals == ["terminate", "kill"]
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert any(command.startswith("scancel") for command in commands)
        assert any(command.startswith("squeue") for command in commands)

    def test_a_cancellation_watcher_boundary_failure_interrupts_and_settles(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        launcher, _processes, probe = _simulated(threads, process)

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=_WaitFailureEvent(),
            )
        )

        assert outcome.termination_confirmed
        assert isinstance(outcome.failure, OSError)
        assert "terminate" in process.signals
        assert "kill" in process.signals
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert any(command.startswith("scancel") for command in commands)
        assert any(command.startswith("squeue") for command in commands)

    def test_a_second_event_fault_cannot_hide_the_watcher_failure_or_deadlock(self) -> None:
        """Regression: ``cancelled.set`` raised before the relay was interrupted."""
        simulator = SimThreads()
        faulting = _EventSetFailureThreads(simulator, failing_event=2)
        process = ScriptedPipedProcess(simulator, merge_stderr=True)
        launcher, _processes, probe = _simulated(cast("Threads", faulting), process)

        outcome = simulator.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=_WaitFailureEvent(),
            )
        )

        assert outcome.termination_confirmed
        assert isinstance(outcome.failure, OSError)
        assert "terminate" in process.signals
        assert "kill" in process.signals
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert any(command.startswith("scancel") for command in commands)
        assert any(command.startswith("squeue") for command in commands)

    def test_finished_publication_failure_still_joins_the_watcher_before_return(self) -> None:
        """A failed wakeup cannot leave the cancellation worker behind after settlement."""
        simulator = SimThreads()
        faulting = _EventSetFailureThreads(simulator, failing_event=1)
        process = ScriptedPipedProcess(simulator, merge_stderr=True)
        process.exit(0)
        launcher, _processes, probe = _simulated(cast("Threads", faulting), process)

        outcome, watcher_alive_at_return = simulator.run(
            lambda: (
                launcher.run(
                    GpuJobRequest(gpus=1, time_minutes=5),
                    GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                    write=bytearray().extend,
                    cancel=simulator.event(),
                ),
                faulting.watcher_alive,
            )
        )

        assert outcome.termination_confirmed
        assert isinstance(outcome.failure, _RecordCancellationError)
        assert not watcher_alive_at_return
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert any(command.startswith("scancel") for command in commands)
        assert any(command.startswith("squeue") for command in commands)

    @pytest.mark.parametrize("queued", ["", "possibly-submitted\n"])
    def test_process_start_failure_retains_identity_until_recovery_fences_absence(
        self, queued: str
    ) -> None:
        """The live start path stays unknown; explicit recovery applies the queue fence."""
        simulator = SimThreads()
        reached_start: list[bool] = []

        def fail_after_start_effect(_request: object) -> ScriptedPipedProcess:
            reached_start.append(True)
            raise OSError

        probe = ScriptedProbe(lambda argv: _scheduler_answer(argv, queued=queued))
        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=probe,
            processes=FakePipedProcessLauncher(fail_after_start_effect),
            threads=simulator,
        )
        job_name = "vibesys-test-capture"

        outcome = simulator.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(
                    argv=("work",),
                    cwd=Path("workspace"),
                    env={},
                    job_name=job_name,
                ),
                write=bytearray().extend,
                cancel=simulator.event(),
            )
        )

        assert reached_start == [True]
        assert isinstance(outcome.failure, OSError)
        assert not outcome.termination_confirmed
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert f"scancel --name={job_name} --me" in commands
        assert not any(command.startswith("squeue") for command in commands)

        assert simulator.run(lambda: launcher.reconcile(job_name)) is (not queued)

    def test_empty_queue_before_delayed_submission_does_not_confirm_ambiguous_start(
        self,
    ) -> None:
        """One empty squeue reply is not a fence against a child that submits later."""
        simulator = SimThreads()
        observations = iter(("", "late-job\n"))

        def answer(argv: tuple[str, ...]) -> ProbeResult:
            if argv[-1].startswith("sinfo"):
                return ProbeResult(0, SINFO)
            if "squeue" not in argv[-1]:
                return ProbeResult(0, "")
            return ProbeResult(0, next(observations, "late-job\n"))

        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=ScriptedProbe(answer),
            processes=FakePipedProcessLauncher(lambda _request: (_ for _ in ()).throw(OSError())),
            threads=simulator,
        )

        outcome = simulator.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(
                    argv=("work",),
                    cwd=Path("workspace"),
                    env={},
                    job_name="vibesys-test-capture",
                ),
                write=bytearray().extend,
                cancel=simulator.event(),
            )
        )

        assert not outcome.termination_confirmed
        assert not simulator.run(lambda: launcher.reconcile("vibesys-test-capture"))

    @pytest.mark.parametrize("fault", ["join", "still-alive"])
    def test_unjoined_cancellation_watcher_keeps_the_stable_job_identity_unconfirmed(
        self, fault: str
    ) -> None:
        simulator = SimThreads()
        threads = cast("Threads", _WatcherJoinFailureThreads(simulator, fault))
        process = ScriptedPipedProcess(simulator, merge_stderr=True)
        process.exit(0)
        launcher, _processes, _probe = _simulated(threads, process)

        outcome = simulator.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(
                    argv=("work",),
                    cwd=Path("workspace"),
                    env={},
                    job_name="vibesys-test-capture",
                ),
                write=bytearray().extend,
                cancel=simulator.event(),
            )
        )

        assert not outcome.termination_confirmed
        assert outcome.failure is not None
        if fault == "join":
            assert isinstance(outcome.failure, _WatcherJoinError)

    @pytest.mark.parametrize("fault", ["join", "still-alive"])
    def test_unjoined_old_watcher_cannot_cancel_a_later_stable_name_job(self, fault: str) -> None:
        simulator = SimThreads()
        threads = cast("Threads", _FirstWatcherJoinFailureThreads(simulator, fault))
        first = ScriptedPipedProcess(simulator, merge_stderr=True)
        replacement = ScriptedPipedProcess(simulator, merge_stderr=True)
        started_replacement = simulator.event()
        processes_to_start = [first, replacement]

        def start(_request: object) -> ScriptedPipedProcess:
            process = processes_to_start.pop(0)
            if process is replacement:
                started_replacement.set()
            return process

        processes = FakePipedProcessLauncher(start)
        probe = ScriptedProbe(_scheduler_answer)
        launcher = AgentGpuLauncher(
            _config(), ("slurm-shell",), probe=probe, processes=processes, threads=threads
        )
        delayed_failure = _DelayedWaitFailureEvent(simulator)
        job_name = "vibesys-test-capture"

        def run_job(cancel: Event) -> GpuJobOutcome:
            return launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}, job_name=job_name),
                write=bytearray().extend,
                cancel=cancel,
            )

        def scenario() -> tuple[GpuJobOutcome, int, int, GpuJobOutcome]:
            def end_first_after_watcher_waits() -> None:
                assert delayed_failure.entered.wait(100), "the old watcher did not start"
                first.exit(0)

            finisher = threads.spawn(
                end_first_after_watcher_waits,
                name="finish-old-capture",
                daemon=False,
            )
            old = run_job(cast("Event", delayed_failure))
            finisher.join(100)
            assert not finisher.is_alive()
            assert delayed_failure.entered.is_set()
            before = sum("scancel" in argv[-1] for argv, _timeout in probe.calls)
            replacement_outcome: list[GpuJobOutcome] = []
            replacement_worker = threads.spawn(
                lambda: replacement_outcome.append(run_job(simulator.event())),
                name="replacement-capture",
                daemon=False,
            )
            assert started_replacement.wait(100), "the replacement capture did not start"
            delayed_failure.release.set()
            simulator.sleep(1)
            after = sum("scancel" in argv[-1] for argv, _timeout in probe.calls)
            replacement.exit(0)
            replacement_worker.join(100)
            assert not replacement_worker.is_alive()
            return old, before, after, replacement_outcome[0]

        old, before, after, later = simulator.run(scenario)

        assert not old.termination_confirmed
        assert before == after
        assert later.termination_confirmed

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
        assert {line for line in commands if line.startswith("squeue")} == {
            f"squeue --name={job_name} --me --noheader --format=%i"
        }
        assert process.signals[0] == "terminate"
        assert b"cancel unconfirmed" not in output

    def test_cancel_reports_when_the_named_job_cannot_be_proved_gone(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        probe = ScriptedProbe(lambda argv: _scheduler_answer(argv, queued="still-running\n"))
        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=probe,
            processes=FakePipedProcessLauncher(lambda _start: process),
            threads=threads,
        )
        cancel = threads.event()
        cancel.set()
        output = bytearray()

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=cancel,
            )
        )

        assert not outcome.termination_confirmed
        assert b"cancel unconfirmed" in output

    def test_abnormal_wrapper_exit_is_unconfirmed_while_the_named_job_remains(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.exit(255)
        probe = ScriptedProbe(lambda argv: _scheduler_answer(argv, queued="still-running\n"))
        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=probe,
            processes=FakePipedProcessLauncher(lambda _start: process),
            threads=threads,
        )

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=threads.event(),
            )
        )

        assert outcome.exit_code == 255
        assert not outcome.termination_confirmed
        assert outcome.failure is None
        controller = [
            argv[-1] for argv, _timeout in probe.calls if argv[-1].startswith(("scancel", "squeue"))
        ]
        assert controller[0].startswith("scancel")
        assert all(command.startswith("squeue") for command in controller[1:])

    def test_workload_failure_is_settled_after_scheduler_absence_proof(self) -> None:
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.exit(7)
        probe = ScriptedProbe(_scheduler_answer)
        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=probe,
            processes=FakePipedProcessLauncher(lambda _start: process),
            threads=threads,
        )

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=threads.event(),
            )
        )

        controller = [
            argv[-1] for argv, _timeout in probe.calls if argv[-1].startswith(("scancel", "squeue"))
        ]
        assert outcome.exit_code == 7
        assert outcome.termination_confirmed
        assert outcome.failure is None
        assert controller[0].startswith("scancel")
        assert len(controller) == 7
        assert all(command.startswith("squeue") for command in controller[1:])

    @given(
        interval=st.floats(0.01, 120, allow_nan=False) | st.just(5.0), status=st.integers(1, 255)
    )
    def test_a_failed_command_returns_after_five_queue_checks_at_the_configured_interval(
        self, interval: float, status: int
    ) -> None:
        """The proof window is five intervals of virtual time, 25 s by default, never more."""
        threads = SimThreads()
        process = ScriptedPipedProcess(threads, merge_stderr=True)
        process.exit(status)
        launcher = AgentGpuLauncher(
            _config(queue_check_interval_seconds=interval),
            ("slurm-shell",),
            probe=ScriptedProbe(_scheduler_answer),
            processes=FakePipedProcessLauncher(lambda _start: process),
            threads=threads,
        )

        def run() -> tuple[GpuJobOutcome, float]:
            started = threads.now()
            outcome = launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=bytearray().extend,
                cancel=threads.event(),
            )
            return outcome, threads.now() - started

        outcome, waited = threads.run(run)

        assert (outcome.exit_code, outcome.termination_confirmed) == (status, True)
        assert waited == pytest.approx(5 * interval)

    def test_the_default_queue_check_interval_keeps_the_proof_window_at_25_seconds(self) -> None:
        assert _config().queue_check_interval_seconds * 5 == 25

    @pytest.mark.parametrize("queued", ["", "still-running\n"])
    def test_a_post_submission_relay_failure_stops_and_confirms_the_named_job(
        self, queued: str
    ) -> None:
        confirmed = not queued
        threads = SimThreads()
        process = _ReadFailureProcess(threads, merge_stderr=True)
        probe = ScriptedProbe(lambda argv: _scheduler_answer(argv, queued=queued))
        launcher = AgentGpuLauncher(
            _config(),
            ("slurm-shell",),
            probe=probe,
            processes=FakePipedProcessLauncher(lambda _start: process),
            threads=threads,
        )
        output = bytearray()

        outcome = threads.run(
            lambda: launcher.run(
                GpuJobRequest(gpus=1, time_minutes=5),
                GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
                write=output.extend,
                cancel=threads.event(),
            )
        )

        assert isinstance(outcome.failure, OSError)
        assert outcome.termination_confirmed is confirmed
        assert process.signals == ["terminate", "kill"]
        commands = [argv[-1] for argv, _timeout in probe.calls]
        assert sum(command.startswith("scancel") for command in commands) == 1
        assert sum(command.startswith("squeue") for command in commands) >= 1
        assert (b"cancel unconfirmed" in output) is (not confirmed)


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


def test_without_a_cap_the_number_of_concurrent_jobs_is_unlimited() -> None:
    assert AgentGpuConfig.model_validate(_LIMITS).max_concurrent_jobs is None


@given(cap=st.integers(1, 10_000))
def test_a_positive_concurrent_job_cap_is_accepted(cap: int) -> None:
    config = AgentGpuConfig.model_validate({**_LIMITS, "max_concurrent_jobs": cap})

    assert config.max_concurrent_jobs == cap


@given(cap=st.integers(max_value=0))
def test_a_concurrent_job_cap_below_one_is_rejected_by_name(cap: int) -> None:
    with pytest.raises(ValueError, match=r"max_concurrent_jobs\n.*greater than 0"):
        AgentGpuConfig.model_validate({**_LIMITS, "max_concurrent_jobs": cap})


@pytest.mark.parametrize("cap", [True, 2.0, "2", [2]])
def test_a_concurrent_job_cap_that_is_not_an_integer_is_rejected_by_name(cap: object) -> None:
    with pytest.raises(ValueError, match=r"max_concurrent_jobs\n.*(valid integer|int)"):
        AgentGpuConfig.model_validate({**_LIMITS, "max_concurrent_jobs": cap})
