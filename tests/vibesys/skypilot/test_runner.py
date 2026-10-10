from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.skypilot import (
    ClusterStatus,
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
    build_task_document,
    stable_cluster_name,
)
from vs_sim.api.testing import SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


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
        return ProcessResult(normalized, result.returncode, result.stdout, result.stderr)


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> ProcessResult:
    return ProcessResult(("sky",), returncode, stdout, stderr)


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
    assert active.status is ClusterStatus.UP
    unknown = runner.inspect_cluster("lease")
    assert unknown is not None
    assert unknown.status is ClusterStatus.UNKNOWN
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

    assert cluster.status is ClusterStatus.UP
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

    assert cluster.status is ClusterStatus.UP
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
            _result(stdout=json.dumps({"lease": [{"job_name": "job-token", "job_id": 7}]})),
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

    assert cluster.status is ClusterStatus.UP
    assert len(fake.calls) == 3


@settings(deadline=None, max_examples=40)
@given(inits=st.integers(0, 12), seed=st.one_of(st.none(), st.integers(0, 2**32)))
def test_ensure_polls_through_any_number_of_init_reports(inits: int, seed: int | None) -> None:
    def status(name: str) -> ProcessResult:
        return _result(stdout=json.dumps([{"name": "lease", "status": name}]))

    fake = FakeCommandRunner([status("INIT")] * (inits + 1) + [status("UP")])
    threads = SimThreads(schedule_seed=seed)
    runner = SkyPilotJobRunner(fake, threads=threads)

    cluster = threads.run(lambda: runner.ensure_cluster("lease", _resources(), timeout=None))

    assert cluster.status is ClusterStatus.UP
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
            _result(stdout=json.dumps({"lease": [{"job_name": "job-token", "job_id": 7}]})),
            subprocess.TimeoutExpired(("sky", "logs"), 10),
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
            _result(stdout=json.dumps({"lease": [{"job_name": "job-token", "job_id": 7}]})),
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


def test_malformed_status_output_is_typed() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(stdout="not-json")]))

    with pytest.raises(SkyPilotOutputError):
        runner.inspect_cluster("lease")


def test_control_failure_does_not_expose_process_output() -> None:
    runner = SkyPilotJobRunner(FakeCommandRunner([_result(2, stderr="token=secret-value")]))

    with pytest.raises(SkyPilotControlPlaneError) as caught:
        runner.release("lease")

    assert "secret-value" not in str(caught.value)


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (FileNotFoundError("sky"), SkyPilotCLIError),
        (subprocess.TimeoutExpired(("sky", "status"), 1), SkyPilotTimeoutError),
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
    runner = SkyPilotJobRunner(FakeCommandRunner([subprocess.TimeoutExpired(("sky", "down"), 5)]))

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
    fake = FakeCommandRunner([_result(0, "out")])
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
        [_queue(), _queue({"job_name": "j", "job_id": 7, "status": "succeeded"})]
    )
    runner = SkyPilotJobRunner(fake)

    assert runner.query_job("lease", job_name="j") is None
    found = runner.query_job("lease", job_name="j", job_id=7)
    assert found is not None
    assert (found.job_id, found.job_name) == (7, "j")


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
