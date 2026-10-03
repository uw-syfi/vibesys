from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vs_slurm.api import (
    SlurmBatchHandle,
    SlurmBatchRequest,
    SlurmBatchStage,
    SlurmConfig,
    SlurmConfigError,
    SlurmConnectorTransport,
    SlurmError,
    SlurmFileArtifact,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    SlurmService,
    SlurmTreeArtifact,
    load_slurm_config,
    runtime_content_identity,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def test_runtime_content_identity_is_stable_and_opaque() -> None:
    identity = runtime_content_identity()

    assert identity == runtime_content_identity()
    assert identity.startswith("sha256:")
    assert len(identity) == len("sha256:") + 64


def _config(**overrides: object) -> SlurmConfig:
    values: dict[str, object] = {
        "name": "test-cluster",
        "remote_workspace_root": "/operator/campaign/runs",
        "transport": SlurmConnectorTransport(kind="connector", command=("cluster-connector",)),
        "sbatch_command": ("/operator/bin/sbatch-rr",),
        "sbatch_arguments": ("-p", "accelerators", "-t", "00:20:00", "-N", "1"),
        "transport_timeout_seconds": 20,
    }
    values.update(overrides)
    return SlurmConfig.model_validate(values)


class _FakeConnector:
    def __init__(
        self,
        *,
        job_exit_code: int = 0,
        connector_exit_code: int = 0,
        active_polls: int = 0,
        batch_stage_results: dict[str, dict[str, str]] | None = None,
        content_cache_behavior: str = "normal",
    ) -> None:
        self.requests: list[dict[str, object]] = []
        self.job_exit_code = job_exit_code
        self.connector_exit_code = connector_exit_code
        self.uploaded_script: str | None = None
        self.active_polls = active_polls
        self.batch_stage_results = batch_stage_results or {}
        self.fail_operation = "sync_to" if content_cache_behavior == "upload-failure" else None
        self.race_publish_before_next_publish = content_cache_behavior == "concurrent-publisher"
        self.content_cache_behavior = content_cache_behavior
        self.ready_content_objects: set[str] = set()
        self.locked_content_objects: set[str] = set()

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert tuple(argv) == ("cluster-connector",)
        assert timeout == 20
        assert stdin is not None
        request: dict[str, object] = json.loads(stdin)
        self.requests.append(request)
        if self.connector_exit_code:
            return subprocess.CompletedProcess(argv, self.connector_exit_code, "", "private error")

        if request["operation"] == self.fail_operation:
            response = {"version": 1, "returncode": 17, "stdout": "", "stderr": "private error"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

        stdout = self._perform(request)
        response = {"version": 1, "returncode": 0, "stdout": stdout, "stderr": ""}
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    def _perform(self, request: dict[str, object]) -> str:
        operation = request["operation"]
        if operation == "put":
            local_path = Path(str(request["local_path"]))
            if local_path.suffix == ".sbatch":
                self.uploaded_script = local_path.read_text(encoding="utf-8")
            return ""
        if operation == "exec":
            return self._execute(str(request["command"]))
        if operation == "get":
            local_path = Path(str(request["local_path"]))
            local_path.parent.mkdir(parents=True, exist_ok=True)
            remote_path = str(request["remote_path"])
            path_parts = remote_path.split("/")
            if remote_path.endswith("/.vibesys-slurm-results") and request["kind"] == "tree":
                self._write_batch_results(local_path)
            elif ".vibesys-slurm-results" in path_parts:
                result_index = path_parts[path_parts.index(".vibesys-slurm-results") + 1]
                result_name = path_parts[-1]
                value = self.batch_stage_results.get(result_index, {}).get(result_name, "")
                local_path.write_text(value + ("\n" if value else ""), encoding="utf-8")
            elif remote_path.endswith("exit-code.txt"):
                local_path.write_text(f"{self.job_exit_code}\n", encoding="utf-8")
            elif remote_path.endswith("job.log"):
                local_path.write_text("trusted output\n", encoding="utf-8")
            elif request["kind"] == "file":
                local_path.write_text('{"throughput": 1000}\n', encoding="utf-8")
            else:
                (local_path / "trace.csv").write_text("trace\n", encoding="utf-8")
        return ""

    def _write_batch_results(self, local_path: Path) -> None:
        local_path.mkdir(parents=True, exist_ok=True)
        for result_index, result_files in self.batch_stage_results.items():
            result_root = local_path / result_index
            result_root.mkdir()
            for result_name, value in result_files.items():
                (result_root / result_name).write_text(
                    value + ("\n" if value else ""), encoding="utf-8"
                )

    def _execute(self, command: str) -> str:
        tokens = shlex.split(command)
        cache_response = self._content_cache_response(tokens)
        if cache_response is not None:
            return cache_response
        if "sbatch-rr" in command:
            return "Submitted batch job 1234\n"
        if command.startswith("squeue") and self.active_polls:
            self.active_polls -= 1
            return "RUNNING\n"
        if command.startswith("sacct"):
            return "COMPLETED 0:0\n"
        return ""

    def _content_cache_response(self, tokens: list[str]) -> str | None:
        if len(tokens) > 4 and tokens[:3] == ["if", "[", "-f"]:
            return "READY" if tokens[3] in self.ready_content_objects else "MISSING"
        if tokens[:3] == ["for", "attempt", "in"]:
            if self.content_cache_behavior == "cache-busy":
                return "BUSY"
            ready_path = tokens[tokens.index("-f") + 1]
            if self.race_publish_before_next_publish:
                self.ready_content_objects.add(ready_path)
                self.race_publish_before_next_publish = False
                return "READY"
            if ready_path in self.ready_content_objects:
                return "READY"
            target = tokens[tokens.index("mv") + 4]
            self.ready_content_objects.add(f"{target}/ready")
            return "PUBLISHED"
        for index, word in enumerate(tokens[:-3]):
            if word == "mv" and tokens[index + 1 : index + 3] == ["-T", "--"]:
                self.ready_content_objects.add(f"{tokens[index + 3]}/ready")
        if "rmdir" in tokens:
            self.locked_content_objects.discard(tokens[tokens.index("rmdir") + 2])
        return None


class _FilesystemConnector:
    """Faithful local filesystem implementation of the connector protocol."""

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        assert tuple(argv) == ("cluster-connector",)
        assert stdin is not None
        request: dict[str, object] = json.loads(stdin)
        operation = request["operation"]
        if operation == "sync_to":
            self._sync_tree(request)
            return self._response(argv)
        if operation == "put":
            source = Path(str(request["local_path"]))
            destination = Path(str(request["remote_path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return self._response(argv)
        if operation == "exec":
            command = str(request["command"])
            if "sbatch-rr" in command:
                return self._response(argv, stdout="Submitted batch job 1234\n")
            completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-092712 [S603]; this faithful connector test must execute the exact production-generated shell program to verify its symlink behavior; inspecting the string or replacing the shell would not exercise the security boundary.
                ("/bin/bash", "-c", command),
                check=False,
                capture_output=True,
                text=True,
            )
            return self._response(
                argv,
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        raise AssertionError(operation)

    @staticmethod
    def _response(
        argv: Sequence[str],
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> subprocess.CompletedProcess[str]:
        response = {
            "version": 1,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(response), "")

    @staticmethod
    def _sync_tree(request: dict[str, object]) -> None:
        source = Path(str(request["local_dir"]))
        destination = Path(str(request["remote_dir"]))
        if bool(request["delete"]) and destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)


def test_external_config_is_composable_and_rejects_unknown_slurm_settings(
    tmp_path: Path,
) -> None:
    path = tmp_path / "slurm.toml"
    path.write_text(
        """[slurm]
name = "research-cluster"
remote_workspace_root = "/operator/runs"
sbatch_arguments = ["-p", "accelerators"]
api_key = "do-not-print-this-value"

[slurm.transport]
kind = "ssh"
host = "cluster.example"

[vibesys]
remote_python = "/operator/python"
""",
        encoding="utf-8",
    )

    with pytest.raises(SlurmConfigError, match="api_key") as error:
        load_slurm_config(path)
    assert "do-not-print-this-value" not in str(error.value)


def test_external_config_loads_only_scheduler_and_transport_policy(tmp_path: Path) -> None:
    path = tmp_path / "slurm.toml"
    path.write_text(
        """[slurm]
name = "research-cluster"
remote_workspace_root = "/operator/runs"
sbatch_command = ["/operator/bin/sbatch-rr"]
sbatch_arguments = ["-p", "accelerators", "-N", "1"]

[slurm.transport]
kind = "ssh"
host = "cluster.example"

[vibesys]
accuracy_arguments = ["--endpoint", "private"]
""",
        encoding="utf-8",
    )

    config = load_slurm_config(path)

    assert config.transport.kind == "ssh"
    assert config.transport.host == "cluster.example"
    assert config.sbatch_command == ("/operator/bin/sbatch-rr",)
    assert config.sbatch_arguments == ("-p", "accelerators", "-N", "1")


def test_runner_uses_versioned_connector_protocol_and_collects_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    file_output = tmp_path / "results" / "metrics.json"
    tree_output = tmp_path / "profile"
    connector = _FakeConnector()

    result = SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "run_01").run(
        SlurmJobRequest(
            workspace=workspace,
            command=("python", "benchmark.py"),
            file_artifacts=(SlurmFileArtifact("metrics.json", file_output),),
            tree_artifacts=(SlurmTreeArtifact("profile", tree_output),),
        )
    )

    assert result.job_id == "1234"
    assert result.exit_code == 0
    assert result.output == "trusted output\n"
    assert json.loads(file_output.read_text(encoding="utf-8")) == {"throughput": 1000}
    assert (tree_output / "trace.csv").read_text(encoding="utf-8") == "trace\n"
    assert connector.requests[0] == {
        "version": 1,
        "operation": "exec",
        "command": (
            "mkdir -p /operator/campaign/runs/test-cluster/run_01 "
            "/operator/campaign/runs/test-cluster/.vibesys-content-cache"
        ),
    }
    sync_request = next(
        request for request in connector.requests if request["operation"] == "sync_to"
    )
    assert sync_request["operation"] == "sync_to"
    assert ".tmp.run_01.workspace/payload" in str(sync_request["remote_dir"])
    assert sync_request["delete"] is True
    assert sync_request["excludes"] == [".git/"]
    assert all(request["version"] == 1 for request in connector.requests)


class _FakeSshProcess:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None, float]] = []

    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        command = tuple(argv)
        self.calls.append((command, stdin, timeout))
        if command[0] == "ssh":
            remote_command = command[-1]
            if "sbatch" in remote_command:
                stdout = "Submitted batch job 4567\n"
            elif remote_command.startswith("sacct"):
                stdout = "COMPLETED 0:0\n"
            elif "for attempt in" in remote_command:
                stdout = "PUBLISHED"
            elif "printf 'READY'" in remote_command:
                stdout = "MISSING"
            else:
                stdout = ""
            return subprocess.CompletedProcess(argv, 0, stdout, "")
        source, destination = command[-2:]
        if source.endswith("exit-code.txt"):
            Path(destination).write_text("0\n", encoding="utf-8")
        elif source.endswith("job.log"):
            Path(destination).write_text("ssh output\n", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, "", "")


def test_runner_uses_builtin_ssh_and_rsync_transport(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    process = _FakeSshProcess()
    config = _config(
        transport={
            "kind": "ssh",
            "host": "cluster.example",
            "ssh_command": ["ssh", "-F", "/operator/ssh-config"],
            "rsync_command": ["rsync"],
        }
    )

    result = SlurmJobRunner(config, process=process, invocation_id=lambda: "ssh_01").run(
        SlurmJobRequest(workspace=workspace, command=("true",))
    )

    assert result.job_id == "4567"
    assert result.output == "ssh output\n"
    assert process.calls[0] == (
        (
            "ssh",
            "-F",
            "/operator/ssh-config",
            "--",
            "cluster.example",
            "mkdir -p /operator/campaign/runs/test-cluster/ssh_01 "
            "/operator/campaign/runs/test-cluster/.vibesys-content-cache",
        ),
        None,
        20,
    )
    sync_argv = next(call[0] for call in process.calls if call[0][0] == "rsync")
    assert sync_argv[:7] == (
        "rsync",
        "-a",
        "--delete",
        "--exclude=.git/",
        "-e",
        "ssh -F /operator/ssh-config",
        "--",
    )
    assert ".tmp.ssh_01.workspace/payload/" in sync_argv[-1]


def test_builtin_ssh_does_not_consume_embedding_process_stdin(tmp_path: Path) -> None:
    """The transport must not compete with an embedding stdio protocol for stdin."""
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    transport = tmp_path / "transport.py"
    transport.write_text(
        textwrap.dedent(
            """
            import sys

            if sys.stdin.read(1):
                raise SystemExit(99)
            command = sys.argv[-1] if "--" in sys.argv else ""
            if "for attempt in" in command:
                print("PUBLISHED", end="")
            elif "printf 'READY'" in command:
                print("MISSING", end="")
            elif "sbatch" in command:
                print("Submitted batch job 4567", end="")
            """
        ),
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            from vs_slurm.api import SlurmConfig, SlurmJobRequest, SlurmJobRunner

            config = SlurmConfig.model_validate({{
                "name": "test-cluster",
                "remote_workspace_root": "/operator/campaign/runs",
                "transport": {{
                    "kind": "ssh",
                    "host": "cluster.example",
                    "ssh_command": [{str(sys.executable)!r}, {str(transport)!r}],
                    "rsync_command": [{str(sys.executable)!r}, {str(transport)!r}],
                }},
                "sbatch_command": ["sbatch"],
                "transport_timeout_seconds": 20,
            }})
            handle = SlurmJobRunner(config, invocation_id=lambda: "stdin_01").submit(
                SlurmJobRequest(workspace=Path({str(workspace)!r}), command=("true",))
            )
            print(handle.job_id)
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(  # noqa: S603  # lint-waiver: LW-930048 [S603]; the test invokes the current interpreter with a generated path and no shell; a subprocess wrapper would hide the public process boundary without improving safety.
        [sys.executable, str(driver)],
        input="embedding-protocol-frame\n",
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4567"


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_public_job_handle_recovers_without_resubmission(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(active_polls=4)
    clock = _FakeClock()
    runner = SlurmJobRunner(
        _config(poll_interval_seconds=1),
        process=connector,
        invocation_id=lambda: "recover_01",
        clock=clock,
        pause=clock.advance,
    )

    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))
    recovered_handle = type(handle).model_validate_json(handle.model_dump_json())
    bounded = runner.wait(recovered_handle, timeout_seconds=2)

    assert bounded.status == SlurmJobStatus.RUNNING
    assert bounded.timed_out is True
    assert not any(
        request.get("operation") == "exec" and "scancel" in str(request.get("command"))
        for request in connector.requests
    )

    connector.active_polls = 0
    resumed = SlurmJobRunner(_config(), process=connector).wait(recovered_handle)
    result = SlurmJobRunner(_config(), process=connector).collect(recovered_handle)

    assert resumed.status == SlurmJobStatus.COMPLETED
    assert resumed.timed_out is False
    assert result.job_id == handle.job_id
    assert result.exit_code == 0
    assert (
        sum(
            "sbatch-rr" in str(request.get("command"))
            for request in connector.requests
            if request.get("operation") == "exec"
        )
        == 1
    )


def test_cancel_uses_existing_handle_without_resubmitting(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(active_polls=1)
    runner = SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "cancel_01")
    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    runner.cancel(handle)

    assert any(
        request.get("operation") == "exec" and "scancel 1234" in str(request.get("command"))
        for request in connector.requests
    )
    assert (
        sum(
            "sbatch-rr" in str(request.get("command"))
            for request in connector.requests
            if request.get("operation") == "exec"
        )
        == 1
    )


def test_recovered_handle_is_bound_to_cluster_and_validated(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector()
    runner = SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "bound_01")
    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    other_cluster = SlurmJobRunner(
        _config(remote_workspace_root="/operator/other-runs"), process=connector
    )
    with pytest.raises(SlurmError, match="does not belong to this cluster"):
        other_cluster.poll(handle)
    with pytest.raises(ValidationError, match="job_id"):
        type(handle).model_validate(handle.model_dump() | {"job_id": "1234;scancel"})
    with pytest.raises(ValidationError):
        type(handle).model_validate(
            handle.model_dump()
            | {"remote_log_path": "/operator/campaign/runs/test-cluster/bound_01/sub/job.log"}
        )


def test_bounded_wait_requires_positive_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(active_polls=1)
    runner = SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "timeout_01")
    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    with pytest.raises(SlurmError, match="greater than zero"):
        runner.wait(handle, timeout_seconds=0)

    assert not any(
        request.get("operation") == "exec" and "scancel" in str(request.get("command"))
        for request in connector.requests
    )


class _TimedOutProcess:
    def __call__(
        self, argv: Sequence[str], *, stdin: str | None, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del stdin
        raise subprocess.TimeoutExpired(argv, timeout)


def test_builtin_transport_enforces_hard_process_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    config = _config(transport={"kind": "ssh", "host": "cluster.example"})

    with pytest.raises(SlurmError, match="transport timed out during exec"):
        SlurmJobRunner(config, process=_TimedOutProcess()).run(
            SlurmJobRequest(workspace=workspace, command=("true",))
        )


def test_service_and_setup_are_request_policy(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector()
    service = SlurmService(
        command=("python", "-m", "server", "--port", "VIBESYS_DYNAMIC_PORT"),
        readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health",
        startup_timeout_seconds=30,
    )

    SlurmJobRunner(_config(), process=connector).run(
        SlurmJobRequest(
            workspace=workspace,
            command=("python", "check.py", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT"),
            setup_script="/operator/setup.sh",
            service=service,
        )
    )

    assert connector.uploaded_script is not None
    assert "source /operator/setup.sh" in connector.uploaded_script
    assert 'python -m server --port "${PORT}"' in connector.uploaded_script
    assert '"http://127.0.0.1:${PORT}"' in connector.uploaded_script


@pytest.mark.parametrize("path", ["/outside/out.json", "../out.json", "nested/../out.json"])
def test_artifact_paths_are_candidate_relative(path: str, tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    with pytest.raises(SlurmError, match="candidate-relative"):
        SlurmJobRunner(_config()).run(
            SlurmJobRequest(
                workspace=workspace,
                command=("true",),
                file_artifacts=(SlurmFileArtifact(path, tmp_path / "out"),),
            )
        )


def test_connector_failure_does_not_expose_output(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(connector_exit_code=23)

    with pytest.raises(SlurmError, match="exit code 23") as error:
        SlurmJobRunner(_config(), process=connector).run(
            SlurmJobRequest(workspace=workspace, command=("true",))
        )
    assert "private error" not in str(error.value)


def test_nonzero_job_skips_output_artifact_collection(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    output = tmp_path / "metrics.json"
    connector = _FakeConnector(job_exit_code=2)

    result = SlurmJobRunner(_config(), process=connector).run(
        SlurmJobRequest(
            workspace=workspace,
            command=("false",),
            file_artifacts=(SlurmFileArtifact("metrics.json", output),),
        )
    )

    assert result.exit_code == 2
    assert not output.exists()


def test_batch_runs_ordered_stages_in_one_allocation_and_stops_after_failure(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    metrics = tmp_path / "metrics.json"
    connector = _FakeConnector(
        active_polls=1,
        batch_stage_results={
            "phases": {
                "setup-seconds.txt": "0",
                "service-startup-seconds.txt": "1",
            },
            "0000": {
                "exit-code.txt": "0",
                "stdout.txt": "accuracy passed",
                "stderr.txt": "",
                "elapsed-seconds.txt": "2",
            },
            "0001": {
                "exit-code.txt": "7",
                "stdout.txt": "benchmark started",
                "stderr.txt": "benchmark failed",
                "elapsed-seconds.txt": "3",
            },
            "0002": {
                "exit-code.txt": "SKIPPED",
                "stdout.txt": "",
                "stderr.txt": "",
                "elapsed-seconds.txt": "0",
            },
        },
    )
    service = SlurmService(
        command=("python", "-m", "server", "--port", "VIBESYS_DYNAMIC_PORT"),
        readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health",
        startup_timeout_seconds=5,
    )
    clock = _FakeClock()
    runner = SlurmJobRunner(
        _config(),
        process=connector,
        invocation_id=lambda: "batch_01",
        clock=clock,
        pause=clock.advance,
    )

    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            service=service,
            stages=(
                SlurmBatchStage(
                    name="accuracy",
                    command=(
                        "python",
                        "accuracy.py",
                        "--endpoint",
                        "http://127.0.0.1:VIBESYS_DYNAMIC_PORT",
                    ),
                    file_artifacts=(SlurmFileArtifact("metrics.json", metrics),),
                ),
                SlurmBatchStage(
                    name="benchmark",
                    command=("python", "benchmark.py"),
                    timeout_seconds=13,
                ),
                SlurmBatchStage(name="profile", command=("python", "profile.py")),
            ),
        )
    )
    wait_result = runner.wait_batch(handle)
    assert wait_result.timed_out is False
    recovered_handle = SlurmBatchHandle.model_validate_json(wait_result.handle.model_dump_json())
    result = runner.collect_batch(recovered_handle)

    submissions = [
        request
        for request in connector.requests
        if request["operation"] == "exec" and "sbatch-rr" in str(request["command"])
    ]
    assert len(submissions) == 1
    batch_result_gets = [
        request
        for request in connector.requests
        if request["operation"] == "get"
        and str(request["remote_path"]).endswith("/.vibesys-slurm-results")
    ]
    assert len(batch_result_gets) == 1
    assert batch_result_gets[0]["kind"] == "tree"
    assert result.job_exit_code == 0
    assert [(stage.name, stage.exit_code, stage.skipped) for stage in result.stages] == [
        ("accuracy", 0, False),
        ("benchmark", 7, False),
        ("profile", None, True),
    ]
    assert result.stages[0].stdout == "accuracy passed\n"
    assert result.stages[1].stderr == "benchmark failed\n"
    assert result.stages[0].elapsed_seconds == 2
    assert result.stages[1].elapsed_seconds == 3
    assert result.stages[2].elapsed_seconds is None
    assert result.stages[0].artifacts[0].local_path == metrics
    assert not result.stages[1].artifacts
    assert not result.stages[2].artifacts
    assert json.loads(metrics.read_text(encoding="utf-8")) == {"throughput": 1000}
    assert result.phase_timings_seconds["staging"] == 0
    assert result.phase_timings_seconds["submission"] == 0
    assert result.phase_timings_seconds["scheduler_wait_observation"] == 5
    assert result.phase_timings_seconds["setup"] == 0
    assert result.phase_timings_seconds["service_startup"] == 1
    assert result.phase_timings_seconds["stage:accuracy"] == 2
    assert result.phase_timings_seconds["stage:benchmark"] == 3
    assert "collection" in result.phase_timings_seconds
    assert result.content_cache_hits == 0

    script = connector.uploaded_script
    assert script is not None
    assert script.count("service_pid=$!") == 1
    assert "export PORT=" in script
    assert "http://127.0.0.1:${PORT}" in script
    assert script.index("accuracy.py") < script.index("benchmark.py") < script.index("profile.py")
    assert "timeout --signal=TERM --kill-after=5s 13s python benchmark.py" in script
    assert "exit 0" in script

    handle_document = handle.model_dump(mode="json")
    handle_document["stages"][0]["file_artifacts"][0]["remote_path"] = "../../outside"
    with pytest.raises(ValidationError):
        SlurmBatchHandle.model_validate(handle_document)


def test_batch_result_carries_the_service_log_tail_with_repeats_collapsed(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    warnings = ["warning: cache record A", "warning: cache record B"]
    progress = [f"decode: {step} steps, 120 ms/step" for step in range(0, 4000, 100)]
    log_lines = [line for step in progress for line in (*warnings, step)] + warnings
    connector = _FakeConnector(
        active_polls=0,
        batch_stage_results={
            "phases": {
                "setup-seconds.txt": "0",
                "service-startup-seconds.txt": "1",
                "service-log-tail.txt": "\n".join(log_lines),
            },
            "0000": {
                "exit-code.txt": "124",
                "stdout.txt": "",
                "stderr.txt": "",
                "elapsed-seconds.txt": "600",
            },
        },
    )
    clock = _FakeClock()
    runner = SlurmJobRunner(
        _config(),
        process=connector,
        invocation_id=lambda: "batch_tail",
        clock=clock,
        pause=clock.advance,
    )
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            service=SlurmService(
                command=("python", "-m", "server", "--port", "VIBESYS_DYNAMIC_PORT"),
                readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health",
                startup_timeout_seconds=5,
            ),
            stages=(SlurmBatchStage(name="benchmark", command=("python", "benchmark.py")),),
        )
    )

    result = runner.collect_batch(runner.wait_batch(handle).handle)

    tail = result.service_log_tail.splitlines()
    assert len(tail) == 40
    assert len(set(tail)) == len(tail)
    assert tail[-2:] == warnings
    assert tail[:-2] == progress[-38:]
    script = connector.uploaded_script
    assert script is not None
    assert "tail -n 400 .vs-slurm-service.log > " in script


def test_batch_rejects_duplicate_or_unsafe_stage_names(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    runner = SlurmJobRunner(_config())

    with pytest.raises(SlurmError, match="unique safe names"):
        runner.submit_batch(
            SlurmBatchRequest(
                workspace=workspace,
                stages=(
                    SlurmBatchStage(name="accuracy", command=("true",)),
                    SlurmBatchStage(name="accuracy", command=("true",)),
                ),
            )
        )

    with pytest.raises(SlurmError, match="positive integer"):
        runner.submit_batch(
            SlurmBatchRequest(
                workspace=workspace,
                stages=(
                    SlurmBatchStage(
                        name="accuracy",
                        command=("true",),
                        timeout_seconds=0,
                    ),
                ),
            )
        )


def test_content_addressed_staging_reuses_objects_and_keeps_workspaces_fresh(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("version = 1\n", encoding="utf-8")
    support = tmp_path / "support"
    support.mkdir()
    (support / "helper.py").write_text("value = 1\n", encoding="utf-8")
    connector = _FakeConnector()
    invocation_ids = iter(("cache_01", "cache_02", "cache_03", "cache_04"))
    runner = SlurmJobRunner(
        _config(),
        process=connector,
        invocation_id=lambda: next(invocation_ids),
    )

    first = runner.submit(
        SlurmJobRequest(
            workspace=workspace,
            command=("true",),
            support_trees={".vibesys-support": support},
        )
    )
    second = runner.submit(
        SlurmJobRequest(
            workspace=workspace,
            command=("true",),
            support_trees={".vibesys-support": support},
        )
    )

    assert first.remote_workspace != second.remote_workspace
    assert first.content_cache_hits == 0
    assert second.content_cache_hits == 2
    sync_requests = [request for request in connector.requests if request["operation"] == "sync_to"]
    assert len(sync_requests) == 2
    commands = [str(request.get("command", "")) for request in connector.requests]
    assert sum("rsync -a --chmod=Du+w,Fu+w --delete" in command for command in commands) >= 4
    publish_commands = [command for command in commands if "mv -T --" in command]
    assert len(publish_commands) == 2
    assert all(command.index("touch ") < command.index("mv -T --") for command in publish_commands)

    (workspace / "candidate.py").write_text("version = 2\n", encoding="utf-8")
    third = runner.submit(
        SlurmJobRequest(
            workspace=workspace,
            command=("true",),
            support_trees={".vibesys-support": support},
        )
    )
    assert third.remote_workspace != first.remote_workspace
    assert third.content_cache_hits == 1
    assert (
        len([request for request in connector.requests if request["operation"] == "sync_to"]) == 3
    )


def test_support_staging_replaces_a_destination_symlink_without_following_it(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep\n", encoding="utf-8")
    (workspace / ".vibesys-support").symlink_to(outside, target_is_directory=True)
    support = tmp_path / "support"
    support.mkdir()
    (support / "helper.py").write_text("safe = True\n", encoding="utf-8")
    remote_root = tmp_path / "remote"
    runner = SlurmJobRunner(
        _config(remote_workspace_root=str(remote_root)),
        process=_FilesystemConnector(),
        invocation_id=lambda: "safe_destination",
    )

    handle = runner.submit(
        SlurmJobRequest(
            workspace=workspace,
            command=("true",),
            support_trees={".vibesys-support": support},
        )
    )

    destination = Path(handle.remote_workspace) / ".vibesys-support"
    assert destination.is_dir()
    assert not destination.is_symlink()
    assert (destination / "helper.py").read_text(encoding="utf-8") == "safe = True\n"
    assert sorted(path.name for path in outside.iterdir()) == ["keep.txt"]


def test_support_staging_rejects_a_symlinked_parent_component(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "redirect").symlink_to(outside, target_is_directory=True)
    support = tmp_path / "support"
    support.mkdir()
    (support / "helper.py").write_text("safe = True\n", encoding="utf-8")
    runner = SlurmJobRunner(
        _config(remote_workspace_root=str(tmp_path / "remote")),
        process=_FilesystemConnector(),
        invocation_id=lambda: "safe_parent",
    )

    with pytest.raises(SlurmError, match="transport exec failed"):
        runner.submit(
            SlurmJobRequest(
                workspace=workspace,
                command=("true",),
                support_trees={"redirect/support": support},
            )
        )

    assert not any(outside.iterdir())


def test_failed_content_upload_does_not_publish_a_ready_object(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(content_cache_behavior="upload-failure")

    with pytest.raises(SlurmError, match="exit code 17"):
        SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "failed_upload").submit(
            SlurmJobRequest(workspace=workspace, command=("true",))
        )

    remote_commands = [
        str(request.get("command", ""))
        for request in connector.requests
        if request["operation"] == "exec"
    ]
    assert not any("mv -T --" in command for command in remote_commands)
    assert not connector.ready_content_objects
    assert not connector.locked_content_objects


def test_concurrent_content_publisher_wins_without_loser_deleting_its_object(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("same snapshot\n", encoding="utf-8")
    connector = _FakeConnector(content_cache_behavior="concurrent-publisher")

    handle = SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "race_01").submit(
        SlurmJobRequest(workspace=workspace, command=("true",))
    )

    assert handle.content_cache_hits == 1
    assert (
        len([request for request in connector.requests if request["operation"] == "sync_to"]) == 1
    )
    assert connector.ready_content_objects
    assert not connector.locked_content_objects
    commands = [
        str(request.get("command", ""))
        for request in connector.requests
        if request["operation"] == "exec"
    ]
    assert any("for attempt in" in command for command in commands)
    removed_paths = [
        tokens[index + 3]
        for command in commands
        for tokens in (shlex.split(command),)
        for index in range(len(tokens) - 3)
        if tokens[index : index + 3] == ["rm", "-rf", "--"]
    ]
    assert not any(
        ".vibesys-content-cache/" in path and ".tmp." not in path for path in removed_paths
    )


def test_cache_lock_timeout_is_reported_as_retryable_and_never_deletes_target(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(content_cache_behavior="cache-busy")

    with pytest.raises(SlurmError, match=r"temporarily unavailable.*retry staging"):
        SlurmJobRunner(_config(), process=connector, invocation_id=lambda: "busy_01").submit(
            SlurmJobRequest(workspace=workspace, command=("true",))
        )

    publish = next(
        str(request["command"])
        for request in connector.requests
        if request["operation"] == "exec" and "for attempt in" in str(request["command"])
    )
    assert "owner=%s\\npid=%s\\ncreated=%s" in publish
    assert "[ $((now - created_at)) -ge 300 ]" in publish
    assert "mv -- " in publish
    assert "rm -rf --" in publish
