"""``SkyPilotJobRunner``'s command construction and process handling over fake processes.

Guarantees under test:

* Task documents quote argv exactly once and apply the operator command prefix to
  workload commands only; cluster names derive from effective resources.
* Cluster status and job queue output is parsed strictly: unknown, truncated,
  duplicated or malformed output is a typed failure, never a guess; a job the
  queue reported finished never runs again.
* Waiting on a cluster polls through any number of INIT reports, and honours the
  per-operation deadline.
* A timed-out subprocess is killed as a group, and every drain worker and pipe is
  settled within bounded waits; output is delivered to the sink in full while the
  returned result keeps only the exact bounded suffix.
* Control failures name the operation and exit code and never expose process output.
"""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.skypilot import (
    ClusterUnknown,
    ClusterUp,
    JobEnd,
    JobEnded,
    JobStatus,
    ProcessResult,
    ResolvedSkyPilotResources,
    SkyPilotCLIError,
    SkyPilotClusterNotReadyError,
    SkyPilotControlPlaneError,
    SkyPilotJobRunner,
    SkyPilotJobStateError,
    SkyPilotOutputError,
    SkyPilotTimeoutError,
    SubprocessCommandRunner,
    build_task_document,
    stable_cluster_name,
)
from vs_sim.api import ProcessTimeoutError, Threads
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping, Sequence

    from vs_sim.api import Condition, Event, Lock, ThreadLocal, Worker

_GROUP_PERMISSION_ERROR = "process group is not signalable"


def _resources(**overrides: object) -> ResolvedSkyPilotResources:
    values: dict[str, object] = {
        "profile_name": "test",
        "infra": "slurm/example/gpu",
        "nodes": 1,
        "accelerator_backend": "rocm",
        "accelerator_type": "MI300A",
        "accelerators_per_node": 4,
        "cpus_per_node": 192,
        "exclusive": True,
        "remote_runtime_image": "docker:rocm/pytorch:test",
        "allocation_time": "08:00:00",
        "remote_artifact_root": "/persistent/vibesys",
    }
    values.update(overrides)
    return ResolvedSkyPilotResources.model_validate(values, strict=True)


class FakeCommandRunner:
    def __init__(self, results: Sequence[ProcessResult | BaseException]) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, ...]] = []
        self.task_documents: list[dict[str, object]] = []
        self.on_run: Callable[[tuple[str, ...]], None] | None = None

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        cwd: Path | None = None,
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        del timeout, cwd
        normalized = tuple(argv)
        self.calls.append(normalized)
        if self.on_run is not None:
            self.on_run(normalized)
        if normalized[-1].endswith("task.yaml"):
            self.task_documents.append(yaml.safe_load(Path(normalized[-1]).read_text()))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        if stdout_sink is not None and result.stdout:
            stdout_sink(result.stdout)
        if stderr_sink is not None and result.stderr:
            stderr_sink(result.stderr)
        return ProcessResult(
            normalized,
            result.returncode,
            result.stdout,
            result.stderr,
            result.stdout_omitted_characters,
            result.stderr_omitted_characters,
        )


class _StuckDrainWorker:
    """A drain that consumes its finite join budget but never completes."""

    def __init__(self, threads: _StuckDrainThreads, name: str) -> None:
        self._threads = threads
        self.name = name
        self.join_timeouts: list[float | None] = []

    def is_alive(self) -> bool:
        return not self._threads.drains_released

    def join(self, timeout: float | None = None) -> None:
        self.join_timeouts.append(timeout)
        if self.is_alive() and timeout is not None:
            self._threads.advance(timeout)


class _StuckDrainThreads:
    """A virtual clock whose output workers stay blocked forever."""

    def __init__(self) -> None:
        self._threads = SimThreads()
        self.workers: list[_StuckDrainWorker] = []
        self.drains_released = False

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        del target, daemon
        worker = _StuckDrainWorker(self, name)
        self.workers.append(worker)
        return worker

    def lock(self) -> Lock:
        return self._threads.lock()

    def rlock(self) -> Lock:
        return self._threads.rlock()

    def condition(self, lock: Lock | None = None) -> Condition:
        return self._threads.condition(lock)

    def event(self) -> Event:
        return self._threads.event()

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        return self._threads.local(initial)

    def sleep(self, seconds: float) -> None:
        self._threads.advance(seconds)

    def now(self) -> float:
        return self._threads.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        return self._threads.run_async(main)

    def advance(self, seconds: float) -> None:
        self._threads.advance(seconds)

    def release_drains(self) -> None:
        self.drains_released = True


class _StuckSkyPilotProcess:
    """A process whose leader and inherited output pipes never report completion."""

    def __init__(self, threads: _StuckDrainThreads) -> None:
        self._threads = threads
        self.stdout = StringIO()
        self.stderr = StringIO()
        self.wait_timeouts: list[float | None] = []
        self.group_kills = 0

    def wait(self, timeout: float | None) -> int:
        self.wait_timeouts.append(timeout)
        if timeout is None:
            raise AssertionError
        if len(self.wait_timeouts) > 1:
            self._threads.advance(timeout)
        raise ProcessTimeoutError(("sky",), timeout)

    def kill_group(self) -> None:
        self.group_kills += 1

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        self._threads.release_drains()


class _NormalSkyPilotProcess(_StuckSkyPilotProcess):
    """A leader that exits normally while descendants retain both pipes."""

    def wait(self, timeout: float | None) -> int:
        self.wait_timeouts.append(timeout)
        return 0

    def kill_group(self) -> None:
        super().kill_group()
        self._threads.release_drains()


class _RecordingThreads:
    """Deterministic synchronization with observable worker ownership."""

    def __init__(self) -> None:
        self._threads = SimThreads()
        self.workers: list[Worker] = []

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> Worker:
        worker = self._threads.spawn(target, name=name, daemon=daemon)
        self.workers.append(worker)
        return worker

    def lock(self) -> Lock:
        return self._threads.lock()

    def rlock(self) -> Lock:
        return self._threads.rlock()

    def condition(self, lock: Lock | None = None) -> Condition:
        return self._threads.condition(lock)

    def event(self) -> Event:
        return self._threads.event()

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        return self._threads.local(initial)

    def sleep(self, seconds: float) -> None:
        self._threads.sleep(seconds)

    def now(self) -> float:
        return self._threads.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        return self._threads.run_async(main)

    def run[T](self, main: Callable[[], T]) -> T:
        return self._threads.run(main)


class _BlockingCancellableSink:
    """A bridge-shaped sink whose active transport write ends on cancellation."""

    def __init__(self, threads: Threads) -> None:
        self.started = threads.event()
        self.cancelled = threads.event()
        self.returned = threads.event()

    def __call__(self, _: str) -> None:
        self.started.set()
        assert self.cancelled.wait(timeout=1)
        self.returned.set()

    def cancel(self) -> None:
        self.cancelled.set()


class _BlockingOrdinarySink:
    """A finite ordinary callback controlled entirely by synchronization events."""

    def __init__(self, threads: Threads) -> None:
        self.started = threads.event()
        self.release = threads.event()
        self.returned = threads.event()

    def __call__(self, _: str) -> None:
        self.started.set()
        assert self.release.wait(timeout=1)
        self.returned.set()


class _RecordingCancellableSink:
    """Record every concurrently delivered output segment."""

    def __init__(self) -> None:
        self.segments: list[str] = []

    def __call__(self, data: str) -> None:
        self.segments.append(data)

    def cancel(self) -> None:
        """Delivery never blocks, so cancellation has no work to do."""


class _ExitedSkyPilotProcess:
    """A completed command with one captured stdout segment."""

    def __init__(self, stdout: str = "output\n", stderr: str = "") -> None:
        self.stdout = StringIO(stdout)
        self.stderr = StringIO(stderr)
        self.group_kills = 0

    def wait(self, timeout: float | None) -> int:
        del timeout
        return 0

    def kill_group(self) -> None:
        self.group_kills += 1

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        pass


class _TimeoutAfterSinkStartsProcess:
    """A command that times out only after delivery is blocked in its sink."""

    def __init__(self, sink: _BlockingCancellableSink) -> None:
        self._sink = sink
        self.stdout = StringIO("output\n")
        self.stderr = StringIO()
        self.waits = 0
        self.group_kills = 0

    def wait(self, timeout: float | None) -> int:
        self.waits += 1
        if self.waits == 1:
            assert self._sink.started.wait(timeout=1)
            assert timeout is not None
            raise ProcessTimeoutError(("sky",), timeout)
        return -9

    def kill_group(self) -> None:
        self.group_kills += 1

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        pass


class _HeldPipe(StringIO):
    """A text pipe that reaches EOF only when process cleanup cancels it."""

    def __init__(self, threads: Threads) -> None:
        super().__init__()
        self.read_started = threads.event()
        self._cancelled = threads.event()

    def readline(self, size: int = -1, /) -> str:
        del size
        self.read_started.set()
        assert self._cancelled.wait(timeout=1)
        return ""

    def cancel(self) -> None:
        self._cancelled.set()


class _PermissionDeniedSkyPilotProcess:
    """A timed-out process whose group cannot be signalled by the caller."""

    def __init__(self, threads: Threads) -> None:
        self._threads = threads
        self.stdout = _HeldPipe(threads)
        self.stderr = _HeldPipe(threads)
        self.wait_timeouts: list[float | None] = []
        self.group_kills = 0
        self.output_cancellations = 0

    def wait(self, timeout: float | None) -> int:
        self.wait_timeouts.append(timeout)
        if len(self.wait_timeouts) == 1:
            assert self.stdout.read_started.wait(timeout=1)
            assert self.stderr.read_started.wait(timeout=1)
            assert timeout is not None
            raise ProcessTimeoutError(("sky",), timeout)
        assert timeout is not None
        self._threads.sleep(timeout)
        raise ProcessTimeoutError(("sky",), timeout)

    def kill_group(self) -> None:
        self.group_kills += 1
        raise PermissionError(_GROUP_PERMISSION_ERROR)

    def terminate_group(self) -> None:
        """These runners stop a process group only with SIGKILL."""
        raise AssertionError

    def cancel_output(self) -> None:
        self.output_cancellations += 1
        self.stdout.cancel()
        self.stderr.cancel()


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    return ProcessResult(("sky",), returncode, stdout, stderr)


def test_subprocess_timeout_kills_the_group_and_bounds_all_cleanup_waits() -> None:
    threads = _StuckDrainThreads()
    process = _StuckSkyPilotProcess(threads)
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=7,
    )

    started = threads.now()
    with pytest.raises(ProcessTimeoutError) as caught:
        runner.run(("sky", "status"), timeout=3)

    assert caught.value.timeout == 3
    assert process.group_kills == 1
    assert process.wait_timeouts == [3, 7]
    assert [worker.join_timeouts for worker in threads.workers] == [[0], [0]]
    assert threads.now() - started == 7


def test_normal_exit_with_stuck_drains_reaps_workers_and_closes_pipes_after_group_kill() -> None:
    threads = _StuckDrainThreads()
    process = _NormalSkyPilotProcess(threads)
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=7,
    )

    started = threads.now()
    with pytest.raises(SkyPilotCLIError, match="process group was killed"):
        runner.run(("sky", "status"))

    assert process.group_kills == 1
    assert process.wait_timeouts == [None, 7]
    assert [worker.join_timeouts for worker in threads.workers] == [[7, 7], [0, 7]]
    assert all(not worker.is_alive() for worker in threads.workers)
    assert process.stdout.closed
    assert process.stderr.closed
    assert threads.now() - started == 7


def test_timeout_cancels_blocked_bridge_sink_without_retaining_drain_workers_or_pipes() -> None:
    threads = _RecordingThreads()
    sink = _BlockingCancellableSink(threads)
    process = _TimeoutAfterSinkStartsProcess(sink)
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=1,
    )

    def exercise() -> None:
        with pytest.raises(ProcessTimeoutError):
            runner.run(("sky", "status"), timeout=0, stdout_sink=sink)

    threads.run(exercise)

    assert sink.cancelled.is_set()
    assert sink.returned.is_set()
    assert process.group_kills == 1
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(not worker.is_alive() for worker in threads.workers)


def test_timeout_surfaces_group_kill_permission_error_after_settling_owned_resources() -> None:
    threads = _RecordingThreads()
    process = _PermissionDeniedSkyPilotProcess(threads)
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=1,
    )

    def exercise() -> None:
        with pytest.raises(PermissionError, match="not signalable") as caught:
            runner.run(("sky", "status"), timeout=0)
        assert isinstance(caught.value.__cause__, ProcessTimeoutError)

    threads.run(exercise)

    assert process.group_kills == 1
    assert process.output_cancellations == 1
    assert process.wait_timeouts == [0, 1]
    assert process.stdout.closed
    assert process.stderr.closed
    assert all(not worker.is_alive() for worker in threads.workers)


def test_success_waits_for_slow_ordinary_sink_without_abandoning_a_delivery_worker() -> None:
    threads = _RecordingThreads()
    sink = _BlockingOrdinarySink(threads)
    process = _ExitedSkyPilotProcess()
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        cleanup_timeout=0,
    )
    drains_finished: list[bool] = []

    def release_sink() -> None:
        assert sink.started.wait(timeout=1)
        sky_workers = [worker for worker in threads.workers if worker.name.startswith("sky-")]
        drains_finished.append(
            len(sky_workers) == 2 and all(not worker.is_alive() for worker in sky_workers)
        )
        sink.release.set()

    def exercise() -> ProcessResult:
        threads.spawn(release_sink, name="release-sink", daemon=False)
        return runner.run(("sky", "status"), stdout_sink=sink)

    result = threads.run(exercise)

    assert drains_finished == [True]
    assert sink.returned.is_set()
    assert process.group_kills == 0
    assert result == ProcessResult(("sky", "status"), 0, "output\n", "")


def test_subprocess_result_bounds_multiline_and_unterminated_output_while_sink_gets_all() -> None:
    threads = _RecordingThreads()
    stdout = "old\n" + ("x" * 66_000)
    stderr = ("error\n" * 2_000) + "unterminated"
    process = _ExitedSkyPilotProcess(stdout, stderr)
    stdout_sink = _RecordingCancellableSink()
    stderr_sink = _RecordingCancellableSink()
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        max_output_characters=127,
    )

    result = threads.run(
        lambda: runner.run(
            ("sky", "logs"),
            stdout_sink=stdout_sink,
            stderr_sink=stderr_sink,
        )
    )

    assert result.stdout == stdout[-127:]
    assert result.stderr == stderr[-127:]
    assert result.stdout_omitted_characters == len(stdout) - 127
    assert result.stderr_omitted_characters == len(stderr) - 127
    assert result.stdout_truncated
    assert result.stderr_truncated
    assert "".join(stdout_sink.segments) == stdout
    assert "".join(stderr_sink.segments) == stderr
    assert all(len(segment) <= 64 * 1024 for segment in stdout_sink.segments)


@given(
    stdout=st.text(alphabet=("a", "b", "\n", "界"), max_size=100),
    stderr=st.text(alphabet=("x", "y", "\n", "🙂"), max_size=100),
    cap=st.integers(min_value=0, max_value=32),
)
@settings(max_examples=examples(pr=10, nightly=200))
def test_subprocess_result_is_the_exact_bounded_suffix(stdout: str, stderr: str, cap: int) -> None:
    threads = _RecordingThreads()
    process = _ExitedSkyPilotProcess(stdout, stderr)
    runner = SubprocessCommandRunner(
        threads,
        processes=FakePipedProcessLauncher(lambda _start: process),
        max_output_characters=cap,
    )

    result = threads.run(lambda: runner.run(("sky", "status")))

    assert result.stdout == (stdout[-cap:] if cap else "")
    assert result.stderr == (stderr[-cap:] if cap else "")
    assert result.stdout_omitted_characters == max(0, len(stdout) - cap)
    assert result.stderr_omitted_characters == max(0, len(stderr) - cap)


def test_stable_name_uses_effective_resources_not_profile_alias() -> None:
    first = stable_cluster_name("20260825-unsafe_RUN", _resources(profile_name="one"))
    second = stable_cluster_name("20260825-unsafe_RUN", _resources(profile_name="two"))

    assert first == second
    assert first.startswith("vibesys-2026082-")
    assert len(first) <= 28
    assert stable_cluster_name("run", _resources(nodes=2)) != stable_cluster_name(
        "run", _resources(nodes=1)
    )
    assert stable_cluster_name("same-long-prefix-a", _resources()) != stable_cluster_name(
        "same-long-prefix-b", _resources()
    )


def test_task_document_quotes_argv_once() -> None:
    document = build_task_document(
        _resources(),
        workdir=Path("/local/candidate"),
        command=("python", "check.py", "argument with spaces", "$(not-shell)"),
    )

    assert document == {
        "num_nodes": 1,
        "resources": {
            "infra": "slurm/example/gpu",
            "accelerators": "MI300A:4",
            "cpus": 192,
            "image_id": "docker:rocm/pytorch:test",
        },
        "config": {"slurm": {"sbatch_options": {"exclusive": True, "time": "08:00:00"}}},
        "run": "python check.py 'argument with spaces' '$(not-shell)'",
        "workdir": "/local/candidate",
    }


def test_task_document_prepends_operator_command_prefix_once() -> None:
    document = build_task_document(
        _resources(command_prefix=("srun", "--overlap", "--environment=/path/runtime.toml")),
        command=("python", "benchmark.py", "argument with spaces"),
    )

    assert document["run"] == (
        "srun --overlap --environment=/path/runtime.toml python benchmark.py 'argument with spaces'"
    )


def test_inspect_cluster_parses_json_and_unknown_states() -> None:
    fake = FakeCommandRunner(
        [
            _result(stdout=json.dumps([{"name": "lease", "status": "UP"}])),
            _result(stdout=json.dumps({"clusters": [{"name": "lease", "status": "ODD"}]})),
            _result(stdout="[]"),
        ]
    )
    runner = SkyPilotJobRunner(fake)

    active = runner.inspect_cluster("lease")
    assert active is not None
    assert active.life == ClusterUp()
    unknown = runner.inspect_cluster("lease")
    assert unknown is not None
    assert unknown.life == ClusterUnknown(raw="ODD")
    assert runner.inspect_cluster("lease") is None
    assert fake.calls[0] == (
        "sky",
        "status",
        "--refresh",
        "--output",
        "json",
        "lease",
    )


def test_ensure_reuses_active_cluster_without_launch() -> None:
    fake = FakeCommandRunner([_result(stdout=json.dumps([{"name": "lease", "status": "UP"}]))])

    cluster = SkyPilotJobRunner(fake).ensure_cluster("lease", _resources())

    assert cluster.life == ClusterUp()
    assert len(fake.calls) == 1


def test_ensure_replaces_stopped_cluster() -> None:
    fake = FakeCommandRunner(
        [
            _result(stdout=json.dumps([{"name": "lease", "status": "STOPPED"}])),
            _result(),
            _result(),
        ]
    )

    cluster = SkyPilotJobRunner(fake).ensure_cluster("lease", _resources())

    assert cluster.life == ClusterUp()
    assert fake.calls[1] == ("sky", "down", "-y", "lease")
    assert fake.calls[2][1:7] == ("launch", "-y", "-d", "-c", "lease", fake.calls[2][6])
    assert fake.task_documents == [build_task_document(_resources(), command=("true",))]


def test_launch_bootstrap_does_not_use_workload_command_prefix() -> None:
    fake = FakeCommandRunner([_result()])

    SkyPilotJobRunner(fake).launch("lease", _resources(command_prefix=("srun", "--overlap")))

    assert fake.task_documents[0]["run"] == "true"


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, JobStatus.COMPLETED), (100, JobStatus.APPLICATION_FAILED), (103, JobStatus.CANCELLED)],
)
def test_run_detaches_discovers_job_and_streams_logs(
    tmp_path: Path, returncode: int, expected: JobStatus
) -> None:
    fake = FakeCommandRunner(
        [
            _result(),
            _result(
                stdout=json.dumps(
                    {"lease": [{"job_name": "job-token", "job_id": 7, "status": "RUNNING"}]}
                )
            ),
            _result(returncode, "stdout\n", "stderr\n"),
        ]
    )
    stdout: list[str] = []
    stderr: list[str] = []

    result = SkyPilotJobRunner(fake, job_name_factory=lambda: "job-token").run(
        "lease",
        _resources(),
        workdir=tmp_path,
        command=("python", "benchmark.py"),
        stdout_sink=stdout.append,
        stderr_sink=stderr.append,
    )

    assert result.status is expected
    assert result.sky_exit_code == returncode
    assert result.remote_job_id == 7
    assert stdout == ["stdout\n"]
    assert stderr == ["stderr\n"]
    assert fake.calls[0][:4] == ("sky", "exec", "-d", "lease")
    assert fake.calls[1] == ("sky", "queue", "lease", "--output", "json")
    assert fake.calls[2] == ("sky", "logs", "lease", "7", "--tail", "0")
    assert fake.task_documents[0]["workdir"] == str(tmp_path)
    assert fake.task_documents[0]["name"] == "job-token"


def test_ensure_waits_for_init_to_become_up() -> None:
    fake = FakeCommandRunner(
        [
            _result(stdout=json.dumps([{"name": "lease", "status": "INIT"}])),
            _result(stdout=json.dumps([{"name": "lease", "status": "INIT"}])),
            _result(stdout=json.dumps([{"name": "lease", "status": "UP"}])),
        ]
    )

    threads = SimThreads()
    runner = SkyPilotJobRunner(fake, threads=threads)

    cluster = threads.run(lambda: runner.ensure_cluster("lease", _resources()))

    assert cluster.life == ClusterUp()
    assert len(fake.calls) == 3


@settings(deadline=None, max_examples=examples(pr=10, nightly=200))
@given(inits=st.integers(0, 12), seed=st.one_of(st.none(), st.integers(0, 2**32)))
def test_ensure_polls_through_any_number_of_init_reports(inits: int, seed: int | None) -> None:
    def status(name: str) -> ProcessResult:
        return _result(stdout=json.dumps([{"name": "lease", "status": name}]))

    fake = FakeCommandRunner([status("INIT")] * (inits + 1) + [status("UP")])
    threads = SimThreads(schedule_seed=seed)
    runner = SkyPilotJobRunner(fake, threads=threads)

    cluster = threads.run(lambda: runner.ensure_cluster("lease", _resources(), timeout=None))

    assert cluster.life == ClusterUp()
    assert len(fake.calls) == inits + 2


def test_ensure_rejects_abnormal_init_transition() -> None:
    fake = FakeCommandRunner(
        [
            _result(stdout=json.dumps([{"name": "lease", "status": "INIT"}])),
            _result(stdout=json.dumps([{"name": "lease", "status": "DOWN"}])),
        ]
    )

    with pytest.raises(SkyPilotClusterNotReadyError, match="became DOWN"):
        SkyPilotJobRunner(fake).ensure_cluster("lease", _resources())


def test_run_timeout_preserves_discovered_job_for_recovery(tmp_path: Path) -> None:
    fake = FakeCommandRunner(
        [
            _result(),
            _result(
                stdout=json.dumps(
                    {"lease": [{"job_name": "job-token", "job_id": 7, "status": "RUNNING"}]}
                )
            ),
            ProcessTimeoutError(("sky", "logs"), 10),
            _result(),
        ]
    )

    with pytest.raises(SkyPilotTimeoutError):
        SkyPilotJobRunner(fake, job_name_factory=lambda: "job-token").run(
            "lease",
            _resources(),
            workdir=tmp_path,
            command=("benchmark",),
            timeout=10,
        )

    assert fake.calls[-1] == ("sky", "logs", "lease", "7", "--tail", "0")


@pytest.mark.parametrize("returncode", [2, 101, 102])
def test_run_does_not_classify_cli_or_indeterminate_codes_as_application_failure(
    tmp_path: Path, returncode: int
) -> None:
    fake = FakeCommandRunner(
        [
            _result(),
            _result(
                stdout=json.dumps(
                    {"lease": [{"job_name": "job-token", "job_id": 7, "status": "RUNNING"}]}
                )
            ),
            _result(returncode),
            _result(),
        ]
    )
    expected = SkyPilotJobStateError if returncode in {101, 102} else SkyPilotControlPlaneError

    with pytest.raises(expected):
        SkyPilotJobRunner(fake, job_name_factory=lambda: "job-token").run(
            "lease", _resources(), workdir=tmp_path, command=("benchmark",)
        )

    assert fake.calls[-1] == ("sky", "logs", "lease", "7", "--tail", "0")


def test_cancel_and_release_use_noninteractive_control_commands() -> None:
    fake = FakeCommandRunner([_result(), _result()])
    runner = SkyPilotJobRunner(fake)

    runner.cancel("lease", 7)
    runner.release("lease")

    assert fake.calls == [
        ("sky", "cancel", "-y", "lease", "7"),
        ("sky", "down", "-y", "lease"),
    ]


def test_successful_side_effect_commands_ignore_truncated_diagnostics(tmp_path: Path) -> None:
    truncated = ProcessResult(("sky",), 0, "diagnostic tail", "", 100, 0)
    fake = FakeCommandRunner(
        [
            truncated,
            truncated,
            _queue({"job_name": "job", "job_id": 9, "status": "RUNNING"}),
            _result(stdout="logs"),
            truncated,
            truncated,
        ]
    )
    runner = SkyPilotJobRunner(fake, job_name_factory=lambda: "job")

    assert runner.launch("lease", _resources()).life == ClusterUp()
    result = runner.run("lease", _resources(), workdir=tmp_path, command=("true",))
    runner.cancel("lease", result.remote_job_id)
    runner.release("lease")

    assert result.status is JobStatus.COMPLETED
    assert [call[1] for call in fake.calls] == ["launch", "exec", "queue", "logs", "cancel", "down"]


def test_malformed_status_output_is_typed() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout="not-json")]))

    with pytest.raises(SkyPilotOutputError):
        runner.inspect_cluster("lease")


def test_control_failure_does_not_expose_process_output() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(2, stderr="token=secret-value")]))

    with pytest.raises(SkyPilotControlPlaneError) as caught:
        runner.release("lease")

    assert "secret-value" not in str(caught.value)


def test_inspect_cluster_rejects_truncated_machine_readable_output() -> None:
    truncated = ProcessResult(
        ("sky",),
        0,
        '[{"name": "lease", "status": "UP"}]',
        "",
        stdout_omitted_characters=1,
    )
    runner = SkyPilotJobRunner(FakeCommandRunner([truncated]))

    with pytest.raises(SkyPilotOutputError, match="status output exceeded"):
        runner.inspect_cluster("lease")


def test_query_job_rejects_truncated_machine_readable_output() -> None:
    truncated = ProcessResult(
        ("sky",),
        0,
        '{"lease": [{"job_name": "j", "job_id": 7}]}',
        "",
        stdout_omitted_characters=1,
    )
    runner = SkyPilotJobRunner(FakeCommandRunner([truncated]))

    with pytest.raises(SkyPilotOutputError, match="queue output exceeded"):
        runner.query_job("lease", job_name="j")


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (FileNotFoundError("sky"), SkyPilotCLIError),
        (ProcessTimeoutError(("sky", "status"), 1), SkyPilotTimeoutError),
    ],
)
def test_process_boundary_failures_are_typed(
    failure: BaseException, error: type[SkyPilotCLIError]
) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([failure]))

    with pytest.raises(error):
        runner.inspect_cluster("lease", timeout=1)


def _queue(*jobs: Mapping[str, object], cluster: str = "lease") -> ProcessResult:
    return _result(stdout=json.dumps({cluster: list(jobs)}))


def test_missing_executable_error_names_the_executable() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([FileNotFoundError("sky")]), executable="my-sky")

    with pytest.raises(SkyPilotCLIError, match="'my-sky' was not found"):
        runner.release("lease")


def test_command_timeout_error_reports_the_timeout() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([ProcessTimeoutError(("sky", "down"), 5)]))

    with pytest.raises(SkyPilotTimeoutError, match="timed out after 5 seconds"):
        runner.release("lease", timeout=5)


def test_control_failure_reports_operation_and_exit_code() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(3)]))

    with pytest.raises(SkyPilotControlPlaneError, match="SkyPilot cancel failed with exit code 3"):
        runner.cancel("lease", 7)


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ('{"clusters": "nope"}', "must contain a cluster list"),
        ("7", "must contain a cluster list"),
    ],
)
def test_inspect_cluster_rejects_status_without_cluster_list(stdout: str, message: str) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=stdout)]))

    with pytest.raises(SkyPilotOutputError, match=message):
        runner.inspect_cluster("lease")


def test_inspect_cluster_rejects_invalid_json_with_message() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout="{")]))

    with pytest.raises(SkyPilotOutputError, match="status returned invalid JSON"):
        runner.inspect_cluster("lease")


_WHOLE_STATUS = json.dumps([{"name": "lease", "status": "UP"}])
_WHOLE_QUEUE = json.dumps({"lease": [{"job_name": "j", "job_id": 7, "status": "RUNNING"}]})


@given(cut=st.integers(min_value=0, max_value=len(_WHOLE_STATUS) - 1))
def test_a_cut_status_reply_is_an_error_never_an_absent_cluster(cut: int) -> None:
    """Every proper prefix of a status document is unknown, not "the cluster is gone"."""
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=_WHOLE_STATUS[:cut])]))

    with pytest.raises(SkyPilotOutputError):
        runner.inspect_cluster("lease")


@given(cut=st.integers(min_value=0, max_value=len(_WHOLE_QUEUE) - 1))
def test_a_cut_queue_reply_is_an_error_never_an_absent_job(cut: int) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=_WHOLE_QUEUE[:cut])]))

    with pytest.raises(SkyPilotOutputError):
        runner.query_job("lease", job_name="j")


@pytest.mark.parametrize("stdout", ["{}", '{"error": "boom"}', '{"clusters": null}'])
def test_a_status_object_without_its_cluster_list_is_not_an_absent_cluster(stdout: str) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=stdout)]))

    with pytest.raises(SkyPilotOutputError, match="must contain a cluster list"):
        runner.inspect_cluster("lease")


@pytest.mark.parametrize(
    "stdout",
    [
        pytest.param("[" * 100_000, id="nested-arrays"),
        pytest.param("{" * 100_000, id="nested-objects"),
        pytest.param('{"lease": ' * 50_000, id="nested-lease-objects"),
    ],
)
def test_absurdly_nested_control_output_is_a_typed_error(stdout: str) -> None:
    status = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=stdout)]))
    queue = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=stdout)]))

    with pytest.raises(SkyPilotOutputError):
        status.inspect_cluster("lease")
    with pytest.raises(SkyPilotOutputError):
        queue.query_job("lease", job_name="j")


def test_ensure_rejects_unknown_cluster_status() -> None:
    fake = FakeCommandRunner([_result(stdout=json.dumps([{"name": "lease", "status": "ODD"}]))])

    with pytest.raises(SkyPilotClusterNotReadyError, match="'lease' has an unknown status"):
        SkyPilotJobRunner(fake).ensure_cluster("lease", _resources())


def test_ensure_reports_cluster_that_disappears_while_initializing() -> None:
    fake = FakeCommandRunner(
        [
            _result(stdout=json.dumps([{"name": "lease", "status": "INIT"}])),
            _result(stdout="[]"),
        ]
    )

    with pytest.raises(SkyPilotClusterNotReadyError, match="became absent while initializing"):
        SkyPilotJobRunner(fake).ensure_cluster("lease", _resources())


def test_ensure_times_out_when_cluster_stays_initializing() -> None:
    init = json.dumps([{"name": "lease", "status": "INIT"}])
    fake = FakeCommandRunner([_result(stdout=init) for _ in range(10)])
    threads = SimThreads()
    # Once the first status call has reported INIT, the next one takes the whole 3 s budget (in
    # virtual time), so the wait that follows has nothing left to sleep.
    fake.on_run = lambda _argv: threads.sleep(3) if len(fake.calls) > 1 else None
    runner = SkyPilotJobRunner(fake, threads=threads)

    with pytest.raises(SkyPilotTimeoutError, match="'lease' remained INIT"):
        threads.run(lambda: runner.ensure_cluster("lease", _resources(), timeout=3))

    assert len(fake.calls) == 2


def test_operation_deadline_is_enforced_before_the_next_command() -> None:
    threads = SimThreads()
    fake = FakeCommandRunner([_result(stdout="[]")])
    # The first command takes 100 virtual seconds, longer than the operation's 50 s budget.
    fake.on_run = lambda _argv: threads.sleep(100)
    runner = SkyPilotJobRunner(fake, threads=threads)

    with pytest.raises(SkyPilotTimeoutError, match="operation exceeded its deadline"):
        threads.run(lambda: runner.ensure_cluster("lease", _resources(), timeout=50))


def test_run_rejects_negative_log_tail(tmp_path: Path) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([]))

    with pytest.raises(ValueError, match="log tail must be nonnegative"):
        runner.run("lease", _resources(), workdir=tmp_path, command=("x",), log_tail=-1)


def test_run_with_existing_job_skips_submission_and_reports_job(tmp_path: Path) -> None:
    fake = FakeCommandRunner([ProcessResult(("sky",), 0, "out", "err", 12, 3)])
    started: list[int] = []

    result = SkyPilotJobRunner(fake).run(
        "lease",
        _resources(),
        workdir=tmp_path,
        command=("x",),
        existing_job_id=9,
        job_started=started.append,
        log_tail=5,
    )

    assert started == [9]
    assert result.remote_job_id == 9
    assert result.stdout == "out"
    assert result.stderr == "err"
    assert result.stdout_omitted_characters == 12
    assert result.stderr_omitted_characters == 3
    assert result.stdout_truncated
    assert result.stderr_truncated
    assert fake.calls == [("sky", "logs", "lease", "9", "--tail", "5")]


@pytest.mark.parametrize(
    ("returncode", "expected", "message"),
    [
        (101, SkyPilotJobStateError, "job state code 101 for job 7"),
        (102, SkyPilotJobStateError, "job state code 102 for job 7"),
        (2, SkyPilotControlPlaneError, "logs failed with exit code 2"),
    ],
)
def test_run_log_failures_carry_code_and_job(
    tmp_path: Path, returncode: int, expected: type[Exception], message: str
) -> None:
    fake = FakeCommandRunner([_result(returncode)])

    with pytest.raises(expected, match=message):
        SkyPilotJobRunner(fake).run(
            "lease", _resources(), workdir=tmp_path, command=("x",), existing_job_id=7
        )


@pytest.mark.parametrize(
    ("stdout", "message"),
    [
        ("not-json", "queue returned invalid JSON"),
        ('{"other": []}', "must map the cluster name to a job list"),
        ('{"lease": [1]}', "must map the cluster name to a job list"),
        ('{"lease": [{"job_name": "j", "job_id": "7"}]}', "non-integer job ID"),
        ('{"lease": [{"job_name": "j", "job_id": true}]}', "non-integer job ID"),
        ('{"lease": [{"job_name": "j", "job_id": 7, "status": "???"}]}', "unknown job status"),
    ],
)
def test_query_job_rejects_malformed_queue_output(stdout: str, message: str) -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout=stdout)]))

    with pytest.raises(SkyPilotOutputError, match=message):
        runner.query_job("lease", job_name="j")


def test_query_job_rejects_duplicates_and_changed_ids() -> None:
    job = {"job_name": "j", "job_id": 7}
    fake = FakeCommandRunner([_queue(job, job), _queue(job)])
    runner = SkyPilotJobRunner(fake)

    with pytest.raises(SkyPilotOutputError, match="duplicate jobs named 'j'"):
        runner.query_job("lease", job_name="j")
    with pytest.raises(SkyPilotOutputError, match="'j' changed ID from 3 to 7"):
        runner.query_job("lease", job_name="j", job_id=3)


def test_query_job_returns_none_when_absent_and_parses_status() -> None:
    fake = FakeCommandRunner(
        [_queue(), _queue({"job_name": "j", "job_id": 7, "status": "SUCCEEDED"})]
    )
    runner = SkyPilotJobRunner(fake)

    assert runner.query_job("lease", job_name="j") is None
    found = runner.query_job("lease", job_name="j", job_id=7)
    assert found is not None
    assert (found.job_id, found.job_name) == (7, "j")
    assert found.life == JobEnded(outcome=JobEnd.OK)


def test_run_times_out_when_job_never_appears_in_queue(tmp_path: Path) -> None:
    fake = FakeCommandRunner([_result(), _queue()])
    threads = SimThreads()
    # A queue listing takes the whole 4 s budget (in virtual time) and does not show the job.
    fake.on_run = lambda argv: threads.sleep(4) if "queue" in argv else None
    runner = SkyPilotJobRunner(fake, threads=threads, job_name_factory=lambda: "j")

    with pytest.raises(SkyPilotTimeoutError, match="did not expose job 'j'"):
        threads.run(
            lambda: runner.run("lease", _resources(), workdir=tmp_path, command=("x",), timeout=4)
        )


def test_task_document_rejects_empty_command() -> None:
    with pytest.raises(ValueError, match="command must not be empty"):
        build_task_document(_resources(), command=())


# Regressions for status the runner used to guess: a missing job status read as
# RUNNING, any case was folded to a known status, and a stale listing could take
# a finished job back to running.


def test_query_job_does_not_read_a_missing_status_as_running() -> None:
    runner = SkyPilotJobRunner(
        FakeCommandRunner([_queue({"job_name": "j", "job_id": 7})]),
    )

    with pytest.raises(SkyPilotOutputError, match="unknown job status None"):
        runner.query_job("lease", job_name="j")


_JOB_STATES = [
    "INIT",
    "PENDING",
    "SETTING_UP",
    "RUNNING",
    "SUCCEEDED",
    "FAILED",
    "FAILED_SETUP",
    "FAILED_DRIVER",
    "CANCELLED",
]


@given(status=st.one_of(st.sampled_from(_JOB_STATES), st.text(max_size=14)))
def test_query_job_accepts_exactly_the_documented_spellings(status: str) -> None:
    runner = SkyPilotJobRunner(
        FakeCommandRunner([_queue({"job_name": "j", "job_id": 7, "status": status})]),
    )

    if status in _JOB_STATES:
        found = runner.query_job("lease", job_name="j")
        assert found is not None
        assert found.life.kind != "unknown"
    else:
        with pytest.raises(SkyPilotOutputError, match="unknown job status"):
            runner.query_job("lease", job_name="j")


@given(statuses=st.lists(st.sampled_from(_JOB_STATES), min_size=1, max_size=12))
def test_a_job_the_queue_reported_finished_never_runs_again(statuses: list[str]) -> None:
    fake = FakeCommandRunner(
        [_queue({"job_name": "j", "job_id": 7, "status": status}) for status in statuses]
    )
    runner = SkyPilotJobRunner(fake)
    ended: JobEnded | None = None

    for _ in statuses:
        found = runner.query_job("lease", job_name="j")
        assert found is not None
        if ended is not None:
            assert found.life == ended
        elif isinstance(found.life, JobEnded):
            ended = found.life


def test_cluster_status_is_not_matched_case_insensitively() -> None:
    def runner() -> SkyPilotJobRunner:
        listing = [{"name": "lease", "status": "up"}]
        return SkyPilotJobRunner(FakeCommandRunner([_result(stdout=json.dumps(listing))]))

    cluster = runner().inspect_cluster("lease")

    assert cluster is not None
    assert cluster.life == ClusterUnknown(raw="up")
    with pytest.raises(SkyPilotClusterNotReadyError, match="unknown status 'up'"):
        runner().ensure_cluster("lease", _resources())
