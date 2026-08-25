from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from vibesys.skypilot.config import ResolvedSkyPilotResources
from vibesys.skypilot.runner import (
    ClusterStatus,
    JobStatus,
    ProcessResult,
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

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


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

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,  # noqa: ARG002
        cwd: Path | None = None,  # noqa: ARG002
        stdout_sink: Callable[[str], None] | None = None,
        stderr_sink: Callable[[str], None] | None = None,
    ) -> ProcessResult:
        normalized = tuple(argv)
        self.calls.append(normalized)
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


def test_subprocess_runner_drains_pipes_and_propagates_sink_failure() -> None:
    calls = 0

    def broken_sink(_: str) -> None:
        nonlocal calls
        calls += 1
        raise BrokenPipeError

    with pytest.raises(BrokenPipeError):
        SubprocessCommandRunner().run(
            (
                sys.executable,
                "-c",
                "import sys; [print(i) for i in range(1000)]; print('err', file=sys.stderr)",
            ),
            stdout_sink=broken_sink,
        )

    assert calls == 1000


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


def test_task_document_includes_memory_when_profile_sets_it() -> None:
    document = build_task_document(
        _resources(memory_gb_per_node=480),
        command=("true",),
        use_command_prefix=False,
    )

    assert document["resources"]["memory"] == 480


def test_task_document_omits_memory_when_profile_leaves_it_unset() -> None:
    document = build_task_document(
        _resources(memory_gb_per_node=None),
        command=("true",),
        use_command_prefix=False,
    )

    assert "memory" not in document["resources"]


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


def test_inspect_cluster_skips_cluster_not_found_banner_before_json() -> None:
    # Observed real sky 0.13.0 behavior: `sky status --refresh --output json <name>`
    # for an absent cluster logs an ANSI-styled "Cluster(s) not found" notice to
    # stdout ahead of the pretty-printed JSON array, even under --output json.
    banner = "Cluster(s) not found: \x1b[1mlease\x1b[0m."
    fake = FakeCommandRunner([_result(stdout=f"{banner}\n[]\n")])

    assert SkyPilotJobRunner(fake).inspect_cluster("lease") is None


def test_inspect_cluster_skips_cold_start_banner_lines_before_json() -> None:
    # Observed real sky 0.13.0 behavior on a cold API server: informational
    # lines about starting the local server precede the JSON payload on stdout.
    stdout = (
        "\x1b[2mFailed to connect to SkyPilot API server at "
        "http://127.0.0.1:46580. Starting a local server.\x1b[0m\n"
        "\x1b[0m\x1b[32m\U0001f389 SkyPilot API server started. \x1b[0m\x1b[0m\n"
        + json.dumps([{"name": "lease", "status": "UP"}], indent=2)
        + "\n"
    )
    fake = FakeCommandRunner([_result(stdout=stdout)])

    cluster = SkyPilotJobRunner(fake).inspect_cluster("lease")

    assert cluster is not None
    assert cluster.status is ClusterStatus.UP


def test_inspect_cluster_parses_pretty_printed_json() -> None:
    # sky's --output json uses json.dumps(..., indent=2), not compact JSON.
    stdout = json.dumps([{"name": "lease", "status": "UP"}], indent=2) + "\n"
    fake = FakeCommandRunner([_result(stdout=stdout)])

    cluster = SkyPilotJobRunner(fake).inspect_cluster("lease")

    assert cluster is not None
    assert cluster.status is ClusterStatus.UP


def test_query_job_skips_fetching_banner_before_json() -> None:
    # Observed real sky 0.13.0 behavior: `sky queue --output json <cluster>`
    # logs a "Fetching job queue for: ..." notice to stdout before the JSON.
    banner = "Fetching job queue for: lease"
    payload = json.dumps({"lease": [{"job_name": "job-token", "job_id": 7}]}, indent=2)
    fake = FakeCommandRunner([_result(stdout=f"{banner}\n{payload}\n")])

    job = SkyPilotJobRunner(fake).query_job("lease", job_name="job-token")

    assert job is not None
    assert job.job_id == 7


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

    cluster = SkyPilotJobRunner(fake, sleep=lambda _: None).ensure_cluster("lease", _resources())

    assert cluster.status is ClusterStatus.UP
    assert len(fake.calls) == 3


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
