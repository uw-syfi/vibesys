from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_sim.api import DISCARD, OutputSink, ProcessOutcome, ProcessRun, ProcessSpec, ProcessTimedOut
from vs_sim.api.testing import ManualClock, examples
from vs_slurm.api import (
    PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE,
    SCHEDULER_READ_END,
    SERVICE_NOT_READY_EXIT_CODE,
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
    SlurmSubmissionRejectedError,
    SlurmTransferInterruptedError,
    SlurmTreeArtifact,
    load_slurm_config,
    runtime_content_identity,
    split_framed_scheduler_query,
)

# test-isolation: the public Fake connector's sacct renderer keeps this stub's rows realistic.
from vs_slurm.fake_connector import sacct_row

if TYPE_CHECKING:
    from collections.abc import Sequence


def test_runtime_content_identity_is_stable_and_opaque() -> None:
    identity = runtime_content_identity()

    assert identity == runtime_content_identity()
    assert identity.startswith("sha256:")
    assert len(identity) == len("sha256:") + 64


def _unpaused(seconds: float) -> None:
    """The pause of a wait whose job is terminal at its first poll: it is never called."""
    message = f"the job was not terminal at its first poll (pause of {seconds} s)"
    raise AssertionError(message)


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


def _moves(tokens: Sequence[str]) -> list[tuple[str, str]]:
    """Return the (source, destination) operands of every ``mv`` in a tokenized script."""
    moves: list[tuple[str, str]] = []
    for index, word in enumerate(tokens):
        if word != "mv":
            continue
        operands: list[str] = []
        options_ended = False
        for operand in tokens[index + 1 :]:
            if not options_ended and operand.startswith("-"):
                options_ended = operand == "--"
                continue
            operands.append(operand)
            if len(operands) == 2:
                moves.append((operands[0], operands[1]))
                break
    return moves


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
        self.failed_get_suffix: str | None = None
        # Whether the cluster then answers that the failed download's path is absent
        # (a missing result) or present (the download was cut short).
        self.failed_get_absent = False
        self.race_publish_before_next_publish = content_cache_behavior == "concurrent-publisher"
        self.content_cache_behavior = content_cache_behavior
        self.ready_content_objects: set[str] = set()
        self.locked_content_objects: set[str] = set()

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        argv, stdin = spec.argv, spec.input.decode() or None
        assert tuple(argv) == ("cluster-connector",)
        assert timeout == 20
        assert stdin is not None
        request: dict[str, object] = json.loads(stdin)
        self.requests.append(request)
        if self.connector_exit_code:
            return ProcessOutcome(self.connector_exit_code, b"", b"private error")

        if request["operation"] == self.fail_operation or (
            request["operation"] == "get"
            and self.failed_get_suffix is not None
            and str(request["remote_path"]).endswith(self.failed_get_suffix)
        ):
            response = {"version": 1, "returncode": 17, "stdout": "", "stderr": "private error"}
            return ProcessOutcome(0, json.dumps(response).encode(), b"")

        if (
            request["operation"] == "get"
            and str(request["remote_path"]).endswith("/.vibesys-slurm-results")
            and not self.batch_stage_results
        ):
            response = {"version": 1, "returncode": 1, "stdout": "", "stderr": "no result tree"}
            return ProcessOutcome(0, json.dumps(response).encode(), b"")

        stdout = self._perform(request)
        response = {"version": 1, "returncode": 0, "stdout": stdout, "stderr": ""}
        return ProcessOutcome(0, json.dumps(response).encode(), b"")

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

    def _absent(self, remote_path: str) -> bool:
        """Whether the remote path a failed download named does not exist."""
        if remote_path.endswith("/.vibesys-slurm-results") and not self.batch_stage_results:
            return True
        return (
            self.failed_get_absent
            and self.failed_get_suffix is not None
            and remote_path.endswith(self.failed_get_suffix)
        )

    def _write_batch_results(self, local_path: Path) -> None:
        local_path.mkdir(parents=True, exist_ok=True)
        for result_index, result_files in self.batch_stage_results.items():
            result_root = local_path / result_index
            result_root.mkdir()
            for result_name, value in result_files.items():
                (result_root / result_name).write_text(
                    value + ("\n" if value else ""), encoding="utf-8"
                )

    def _execute(self, framed: str) -> str:
        command, end = split_framed_scheduler_query(framed)
        return self._answer(command) + end

    def _answer(self, command: str) -> str:
        tokens = shlex.split(command)
        if tokens[:3] == ["if", "test", "-e"]:
            return "absent\n" if self._absent(tokens[3].rstrip(";")) else "present\n"
        cache_response = self._content_cache_response(tokens)
        if cache_response is not None:
            return cache_response
        if "sbatch-rr" in command:
            return "Submitted batch job 1234\n"
        if command.startswith("squeue") and self.active_polls:
            self.active_polls -= 1
            return "RUNNING\n"
        if command.startswith("sacct"):
            state = "FAILED" if self.job_exit_code else "COMPLETED"
            return sacct_row(state, f"{self.job_exit_code}:0", parsable="-P" in tokens)
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
            _source, target = _moves(tokens)[0]
            self.ready_content_objects.add(f"{target}/ready")
            return "PUBLISHED"
        for _source, target in _moves(tokens):
            self.ready_content_objects.add(f"{target}/ready")
        if "rmdir" in tokens:
            self.locked_content_objects.discard(tokens[tokens.index("rmdir") + 2])
        return None


class _FilesystemConnector:
    """Faithful local filesystem implementation of the connector protocol."""

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        argv, stdin = spec.argv, spec.input.decode() or None
        del timeout
        assert tuple(argv) == ("cluster-connector",)
        assert stdin is not None
        request: dict[str, object] = json.loads(stdin)
        operation = request["operation"]
        if operation == "sync_to":
            self._sync_tree(request)
            return self._response()
        if operation == "put":
            source = Path(str(request["local_path"]))
            destination = Path(str(request["remote_path"]))
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            return self._response()
        if operation == "exec":
            command = str(request["command"])
            if "sbatch-rr" in command:
                return self._response(stdout="Submitted batch job 1234\n")
            completed = subprocess.run(  # noqa: S603  # lint-waiver: LW-092712 [S603]; this faithful connector test must execute the exact production-generated shell program to verify its symlink behavior; inspecting the string or replacing the shell would not exercise the security boundary.
                ("/bin/bash", "-c", command),
                check=False,
                capture_output=True,
                text=True,
            )
            return self._response(
                returncode=completed.returncode,
                stdout=completed.stdout,
                stderr=completed.stderr,
            )
        raise AssertionError(operation)

    @staticmethod
    def _response(
        *,
        returncode: int = 0,
        stdout: str = "",
        stderr: str = "",
    ) -> ProcessRun:
        response = {
            "version": 1,
            "returncode": returncode,
            "stdout": stdout,
            "stderr": stderr,
        }
        return ProcessOutcome(0, json.dumps(response).encode(), b"")

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


@pytest.mark.parametrize("key", ["host", "ssh_command", "command"])
def test_local_transport_rejects_settings_of_other_transports(tmp_path: Path, key: str) -> None:
    path = tmp_path / "slurm.toml"
    path.write_text(
        f"""[slurm]
name = "research-cluster"
remote_workspace_root = "/shared/runs"

[slurm.transport]
kind = "local"
{key} = ["value"]
""",
        encoding="utf-8",
    )

    with pytest.raises(SlurmConfigError, match=f"transport.local.{key}"):
        load_slurm_config(path)


def test_runner_uses_versioned_connector_protocol_and_collects_artifacts(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    file_output = tmp_path / "results" / "metrics.json"
    tree_output = tmp_path / "profile"
    connector = _FakeConnector()

    result = SlurmJobRunner(
        _config(), processes=connector, invocation_id=lambda: "run_01", clock=ManualClock()
    ).run(
        SlurmJobRequest(
            workspace=workspace,
            command=("python", "benchmark.py"),
            file_artifacts=(SlurmFileArtifact("metrics.json", file_output),),
            tree_artifacts=(SlurmTreeArtifact("profile", tree_output),),
        ),
        pause=_unpaused,
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


def _split_clean_environment(argv: Sequence[str]) -> tuple[dict[str, str] | None, tuple[str, ...]]:
    """Split ``env -i NAME=VALUE... PROGRAM ARGS`` into the environment and the program's argv.

    A program that does not start with ``env -i`` inherits this process's environment: ``None``.
    """
    command = tuple(argv)
    if command[:2] != ("env", "-i"):
        return None, command
    rest = command[2:]
    count = 0
    while count < len(rest) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", rest[count], re.DOTALL):
        count += 1
    return dict(item.split("=", 1) for item in rest[:count]), rest[count:]


class _FakeSshProcess:
    """Answer a transport's processes; *program* is what executes remote commands."""

    def __init__(self, program: str = "ssh") -> None:
        self.calls: list[tuple[tuple[str, ...], str | None, float | None]] = []
        self.environments: list[dict[str, str] | None] = []
        self._program = program

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        argv, stdin = spec.argv, spec.input.decode() or None
        environment, command = _split_clean_environment(argv)
        self.environments.append(environment)
        self.calls.append((command, stdin, timeout))
        if command[0] == self._program:
            remote_command, end = split_framed_scheduler_query(command[-1])
            if "sbatch" in remote_command:
                stdout = "Submitted batch job 4567\n"
            elif remote_command.startswith("sacct"):
                stdout = sacct_row("COMPLETED", "0:0", parsable="-P" in remote_command.split())
            elif "for attempt in" in remote_command:
                stdout = "PUBLISHED"
            elif "printf 'READY'" in remote_command:
                stdout = "MISSING"
            else:
                stdout = ""
            return ProcessOutcome(0, (stdout + end).encode(), b"")
        source, destination = command[-2:]
        if source.endswith("exit-code.txt"):
            Path(destination).write_text("0\n", encoding="utf-8")
        elif source.endswith("job.log"):
            Path(destination).write_text("ssh output\n", encoding="utf-8")
        return ProcessOutcome(0, b"", b"")


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

    result = SlurmJobRunner(
        config, processes=process, invocation_id=lambda: "ssh_01", clock=ManualClock()
    ).run(SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused)

    assert result.job_id == "4567"
    assert result.output == "ssh output\n"
    assert process.calls[0] == (
        (
            "ssh",
            "-F",
            "/operator/ssh-config",
            "-o",
            "BatchMode=yes",
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
        "ssh -F /operator/ssh-config -o BatchMode=yes",
        "--",
    )
    assert ".tmp.ssh_01.workspace/payload/" in sync_argv[-1]


def test_local_transport_runs_commands_and_copies_on_this_host(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    process = _FakeSshProcess(program="/operator/bin/login-shell")
    config = _config(
        transport={
            "kind": "local",
            "shell_command": ["/operator/bin/login-shell", "-c"],
            "rsync_command": ["/operator/bin/rsync"],
        }
    )

    result = SlurmJobRunner(
        config, processes=process, invocation_id=lambda: "local_01", clock=ManualClock()
    ).run(SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused)

    assert result.job_id == "4567"
    assert result.output == "ssh output\n"
    assert process.calls[0][0] == (
        "/operator/bin/login-shell",
        "-c",
        "mkdir -p /operator/campaign/runs/test-cluster/local_01 "
        "/operator/campaign/runs/test-cluster/.vibesys-content-cache",
    )
    copies = [call[0] for call in process.calls if call[0][0] == "/operator/bin/rsync"]
    assert copies
    for argv in copies:
        # No remote shell and no host: both operands are paths on this host.
        assert "-e" not in argv
        source, destination = argv[argv.index("--") + 1 :]
        assert source.startswith("/")
        assert destination.startswith("/")
    assert all(
        call[0][0] in {"/operator/bin/login-shell", "/operator/bin/rsync"} for call in process.calls
    )


def test_the_ssh_transport_leaves_the_environment_to_the_login_session(tmp_path: Path) -> None:
    """sshd supplies a fresh environment; the argv carries no environment and no export option."""
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    process = _FakeSshProcess()

    config = _config(transport={"kind": "ssh", "host": "cluster.example"})
    SlurmJobRunner(
        config, processes=process, invocation_id=lambda: "env_01", clock=ManualClock()
    ).run(SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused)

    assert process.environments
    assert all(environment is None for environment in process.environments)
    assert not any("--export" in call[0][-1] for call in process.calls)


_ENVIRONMENT_NAMES = st.one_of(
    st.sampled_from(
        [
            *("HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ"),
            *("TMPDIR", "SLURM_JOB_ID", "SBATCH_EXPORT", "SALLOC_PARTITION", "SRUN_CPUS_PER_TASK"),
            *("LC_", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "LD_PRELOAD"),
        ]
    ),
    st.from_regex(r"[A-Z][A-Z0-9_]{0,12}", fullmatch=True),
)
_ENVIRONMENT_VALUES = st.text(
    st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), max_size=20
)


@given(host=st.dictionaries(_ENVIRONMENT_NAMES, _ENVIRONMENT_VALUES, max_size=12))
@settings(max_examples=examples(pr=5, nightly=60), deadline=None)
def test_local_transport_processes_get_exactly_the_login_environment(host: dict[str, str]) -> None:
    """Whatever the host holds, a local-transport process gets only the login-like names."""
    process = _FakeSshProcess(program="bash")
    config = _config(transport={"kind": "local", "shell_command": ["bash", "-c"]})

    with tempfile.TemporaryDirectory() as workspace:
        SlurmJobRunner(
            config,
            processes=process,
            invocation_id=lambda: "env_01",
            host_environment=lambda: host,
            clock=ManualClock(),
        ).run(SlurmJobRequest(workspace=Path(workspace), command=("true",)), pause=_unpaused)

    expected = {
        name: value
        for name, value in host.items()
        if name in {"HOME", "USER", "LOGNAME", "SHELL", "PATH", "LANG", "TZ", "TMPDIR"}
        or (name.startswith("LC_") and name != "LC_")
    }
    assert len(process.environments) > 1
    assert all(environment == expected for environment in process.environments)
    submit = next(call[0][-1] for call in process.calls if "sbatch-rr" in call[0][-1])
    assert "--export" not in submit


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
                print("Submitted batch job 4567")
            """
        ),
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(
        textwrap.dedent(
            f"""
            from pathlib import Path
            from vs_sim.api import OsProcesses
            from vs_sim.api.testing import ManualClock
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
            handle = SlurmJobRunner(
                config,
                processes=OsProcesses(),
                invocation_id=lambda: "stdin_01",
                clock=ManualClock(),
            ).submit(
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


def test_public_job_handle_recovers_without_resubmission(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(active_polls=4)
    clock = ManualClock()
    runner = SlurmJobRunner(
        _config(poll_interval_seconds=1),
        processes=connector,
        invocation_id=lambda: "recover_01",
        clock=clock,
    )

    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))
    recovered_handle = type(handle).model_validate_json(handle.model_dump_json())
    bounded = runner.wait(recovered_handle, timeout_seconds=2, pause=clock.advance)

    assert bounded.status == SlurmJobStatus.RUNNING
    assert bounded.timed_out is True
    assert not any(
        request.get("operation") == "exec" and "scancel" in str(request.get("command"))
        for request in connector.requests
    )

    connector.active_polls = 0
    resumed = SlurmJobRunner(_config(), processes=connector, clock=ManualClock()).wait(
        recovered_handle, pause=clock.advance
    )
    result = SlurmJobRunner(_config(), processes=connector, clock=ManualClock()).collect(
        recovered_handle
    )

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
    runner = SlurmJobRunner(
        _config(), processes=connector, invocation_id=lambda: "cancel_01", clock=ManualClock()
    )
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
    runner = SlurmJobRunner(
        _config(), processes=connector, invocation_id=lambda: "bound_01", clock=ManualClock()
    )
    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    other_cluster = SlurmJobRunner(
        _config(remote_workspace_root="/operator/other-runs"),
        processes=connector,
        clock=ManualClock(),
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
    runner = SlurmJobRunner(
        _config(), processes=connector, invocation_id=lambda: "timeout_01", clock=ManualClock()
    )
    handle = runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    with pytest.raises(SlurmError, match="greater than zero"):
        runner.wait(handle, timeout_seconds=0, pause=_unpaused)

    assert not any(
        request.get("operation") == "exec" and "scancel" in str(request.get("command"))
        for request in connector.requests
    )


class _TimedOutProcess:
    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        stdin = spec.input.decode() or None
        del stdin
        return ProcessTimedOut(timeout or 0.0, b"", b"")


def test_builtin_transport_enforces_hard_process_timeout(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    config = _config(transport={"kind": "ssh", "host": "cluster.example"})

    with pytest.raises(SlurmError, match="transport timed out during exec"):
        SlurmJobRunner(config, processes=_TimedOutProcess(), clock=ManualClock()).run(
            SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused
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

    SlurmJobRunner(_config(), processes=connector, clock=ManualClock()).run(
        SlurmJobRequest(
            workspace=workspace,
            command=("python", "check.py", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT"),
            setup_script="/operator/setup.sh",
            service=service,
        ),
        pause=_unpaused,
    )

    assert connector.uploaded_script is not None
    assert "source /operator/setup.sh" in connector.uploaded_script
    assert 'python -m server --port "${PORT}"' in connector.uploaded_script
    assert 'http://127.0.0.1:"${PORT}"' in connector.uploaded_script


def _submitted_service_job(tmp_path: Path, config: SlurmConfig) -> tuple[str, str]:
    """Run one service job on the stub connector; return its job script and sbatch command."""
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector()
    service = SlurmService(
        command=("python", "-m", "server", "--port", "VIBESYS_DYNAMIC_PORT"),
        readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health",
        startup_timeout_seconds=30,
    )
    SlurmJobRunner(config, processes=connector, clock=ManualClock()).run(
        SlurmJobRequest(workspace=workspace, command=("python", "check.py"), service=service),
        pause=_unpaused,
    )
    assert connector.uploaded_script is not None
    submissions = [
        str(request["command"])
        for request in connector.requests
        if request["operation"] == "exec" and "sbatch-rr" in str(request["command"])
    ]
    assert len(submissions) == 1
    return connector.uploaded_script, submissions[0]


@given(grace=st.integers(min_value=1, max_value=20), exit_wait=st.integers(1, 1800))
@settings(max_examples=examples(pr=5, nightly=20), deadline=None)
def test_teardown_ends_every_session_the_job_started_and_outlives_them_within_a_bound(
    grace: int, exit_wait: int
) -> None:
    """Regressions (2026-10-15, 2026-10-16): teardown left processes for Slurm to kill.

    First the script waited unbounded on the service's parent pid only; then it sent
    SIGKILL to the service's group and exited 5 s later, while vLLM workers holding GPU
    memory were still exiting, and Slurm's step kill failed and drained the node. The
    service and the command now each run in a session of their own; teardown signals every
    live process of both, waits for the last one to exit up to the configured exit wait,
    and fails the job naming any left. Running the script under bash, with a worker held
    in its exit, is the real-tier test in ``tests/e2e/test_slurm_service_teardown.py``.
    """
    with tempfile.TemporaryDirectory() as temporary:
        script, _submission = _submitted_service_job(
            Path(temporary),
            _config(
                service_stop_grace_seconds=grace,
                process_exit_wait_seconds=exit_wait,
                sbatch_arguments=(),
            ),
        )

    lines = script.splitlines()
    marked = 'VS_SLURM_JOB_PROCESS="$vs_token" setsid'
    service = lines.index(
        f'{marked} python -m server --port "${{PORT}}" > .vs-slurm-service.log 2>&1 &'
    )
    command = lines.index(f"{marked} python check.py &")
    assert lines[service + 1 : service + 3] == [
        "service_pid=$!",
        'vs_groups="$vs_groups $service_pid"',
    ]
    assert lines[command + 1 : command + 5] == [
        "command_pid=$!",
        'vs_groups="$vs_groups $command_pid"',
        'wait "$command_pid"',
        "job_status=$?",
    ]
    trap = lines.index("trap vs_stop_groups EXIT")
    assert trap < service < command
    assert lines[trap + 1] == "trap 'exit 143' TERM"
    teardown = "\n".join(lines[lines.index("vs_stop_groups() {") : trap])
    term = teardown.index("vs_signal TERM")
    kill = teardown.index("vs_signal KILL")
    assert teardown.index("trap '' TERM") < term
    assert f"deadline=$((SECONDS + {grace}))" in teardown[term:kill]
    assert f"deadline=$((SECONDS + {exit_wait}))" in teardown[kill:]
    assert teardown.count('while vs_scan && [ "$SECONDS" -lt "$deadline" ]') == 2
    failure = teardown[teardown.index('[ -n "$vs_alive$vs_unknown" ] || return 0') :]
    assert f"still alive {exit_wait} s after SIGKILL" in failure
    # A service that never became ready keeps its status; any other job fails with 71.
    kept = failure.index(f'[ "$status" -ne {SERVICE_NOT_READY_EXIT_CODE} ] || return 0')
    assert kept < failure.index(f"exit {PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE}")
    assert f"printf '%s\\n' {PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE} > " in failure[kept:]


@given(grace=st.integers(min_value=1, max_value=20), exit_wait=st.integers(1, 1800))
@settings(max_examples=examples(pr=5, nightly=20), deadline=None)
def test_teardown_bounds_every_read_that_can_block_on_another_process(
    grace: int, exit_wait: int
) -> None:
    """Review (2026-10-16): one unbounded read of /proc/*/environ could hang the teardown.

    Reading another process's environment or command line takes that process's mmap_lock,
    which an exiting or driver-stuck process (the case teardown exists for) can hold
    indefinitely; the script then never reached its deadline, never exited 71, and the job
    outlived its time limit. Every such read now runs under ``timeout -s KILL``, and a read
    that did not finish keeps the teardown waiting (the process is unknown) instead of
    ending the scan. A real-tier test cannot hold another process's mmap_lock from user
    space without privileges, so the script's shape is pinned here.
    """
    with tempfile.TemporaryDirectory() as temporary:
        script, _submission = _submitted_service_job(
            Path(temporary),
            _config(
                service_stop_grace_seconds=grace,
                process_exit_wait_seconds=exit_wait,
                sbatch_arguments=(),
            ),
        )

    readers = [
        line
        for line in script.splitlines()
        if re.search(r"/environ|/cmdline|\bps\b|\bpgrep\b|\bpkill\b", line)
    ]
    assert len(readers) == 2, readers
    assert all(re.search(r"\btimeout -s KILL \d+ (grep|ps) ", line) for line in readers), readers
    scan = script[script.index("vs_scan_once() {") : script.index("vs_scan() {")]
    check = script[script.index("vs_check() {") : script.index("vs_scan_once() {")]
    # A read cut short (124, or 137 when timeout kills itself too) leaves the process
    # unknown, and an unknown process keeps the scan reporting the job as not yet gone.
    assert '124 | 137) vs_unknown="$vs_unknown $1" ;;' in check
    assert scan.rstrip().endswith('[ -n "$vs_alive$vs_unknown" ]\n}')
    teardown = script[script.index("vs_stop_groups() {") :]
    assert '[ -n "$vs_alive$vs_unknown" ] || return 0' in teardown


def test_jobs_are_submitted_without_scheduler_requeue(tmp_path: Path) -> None:
    """VibeSys owns retries: a requeue after a node failure would silently rerun finished work."""
    _script, submission = _submitted_service_job(tmp_path, _config())

    argv = shlex.split(submission.split("&&", 1)[1])
    assert argv[0] == "/operator/bin/sbatch-rr"
    assert argv.index("--no-requeue") > argv.index("00:20:00")
    assert argv[-1].endswith("/run.sbatch")
    assert "--requeue" not in argv


@pytest.mark.parametrize("arguments", [("--requeue",), ("-p", "gpu", "--requeue")])
def test_operator_sbatch_arguments_cannot_turn_requeue_back_on(arguments: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="--requeue"):
        _config(sbatch_arguments=arguments)


@given(grace=st.one_of(st.integers(max_value=0), st.integers(min_value=21)))
def test_service_stop_grace_outside_its_range_is_rejected_by_name(grace: int) -> None:
    """On Slurm's SIGTERM the script's SIGKILL must come 10 s before Slurm's, at KillWait 30 s."""
    with pytest.raises(ValidationError, match="service_stop_grace_seconds"):
        _config(service_stop_grace_seconds=grace)


@given(exit_wait=st.one_of(st.integers(max_value=0), st.integers(min_value=1801)))
def test_process_exit_wait_outside_its_range_is_rejected_by_name(exit_wait: int) -> None:
    with pytest.raises(ValidationError, match="process_exit_wait_seconds"):
        _config(process_exit_wait_seconds=exit_wait, sbatch_arguments=())


def test_teardown_bounds_default_and_load_from_the_operator_file(tmp_path: Path) -> None:
    config = _config()
    assert (config.service_stop_grace_seconds, config.process_exit_wait_seconds) == (20, 120)
    path = tmp_path / "slurm.toml"
    path.write_text(
        """[slurm]
name = "research-cluster"
remote_workspace_root = "/operator/runs"
service_stop_grace_seconds = "10"
process_exit_wait_seconds = 300

[slurm.transport]
kind = "ssh"
host = "cluster.example"
""",
        encoding="utf-8",
    )
    with pytest.raises(SlurmConfigError, match="service_stop_grace_seconds"):
        load_slurm_config(path)
    path.write_text(
        path.read_text(encoding="utf-8").replace('"10"', "10"),
        encoding="utf-8",
    )
    loaded = load_slurm_config(path)
    assert (loaded.service_stop_grace_seconds, loaded.process_exit_wait_seconds) == (10, 300)


def _time_limit_spellings(seconds: int) -> list[str]:
    """Every ``sbatch`` spelling of a time limit of ``seconds`` (a whole number of them)."""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    spellings = [
        f"{seconds // 60}:{secs}",
        f"{hours + 24 * days}:{minutes:02d}:{secs:02d}",
        f"{days}-{hours}:{minutes}:{secs}",
    ]
    if secs == 0:
        spellings.extend([str(seconds // 60), f"{days}-{hours}:{minutes}"])
        if minutes == 0:
            spellings.append(f"{days}-{hours}")
    return spellings


@given(
    limit=st.integers(1, 3 * 86400),
    grace=st.integers(1, 20),
    exit_wait=st.integers(1, 1800),
    spelling=st.data(),
    form=st.sampled_from(["--time={}", "-t{}", "--time {}", "-t {}"]),
)
def test_teardown_must_fit_inside_the_sbatch_time_limit_in_any_spelling(
    limit: int, grace: int, exit_wait: int, spelling: st.DataObject, form: str
) -> None:
    """The script waits up to grace plus exit wait at its end; Slurm's limit must allow it."""
    value = spelling.draw(st.sampled_from(_time_limit_spellings(limit)))
    arguments = ("-p", "gpu", *form.format(value).split(" "))
    settings_ = {"service_stop_grace_seconds": grace, "process_exit_wait_seconds": exit_wait}
    if grace + exit_wait < limit:
        assert _config(sbatch_arguments=arguments, **settings_).process_exit_wait_seconds == (
            exit_wait
        )
    else:
        with pytest.raises(ValidationError, match="process_exit_wait_seconds"):
            _config(sbatch_arguments=arguments, **settings_)


@given(limit_minutes=st.integers(1, 4 * 60), grace=st.integers(1, 20))
def test_the_default_exit_wait_shrinks_to_fit_the_sbatch_time_limit(
    limit_minutes: int, grace: int
) -> None:
    config = _config(sbatch_arguments=("-t", str(limit_minutes)), service_stop_grace_seconds=grace)
    expected = min(120, limit_minutes * 60 - grace - 1)
    assert config.process_exit_wait_seconds == expected
    assert grace + config.process_exit_wait_seconds < limit_minutes * 60


@pytest.mark.parametrize("value", ["0", "INFINITE", "unlimited", "0:0"])
def test_no_time_limit_leaves_teardown_unbounded_by_it(value: str) -> None:
    config = _config(sbatch_arguments=("--time", value), process_exit_wait_seconds=1800)
    assert config.process_exit_wait_seconds == 1800


@pytest.mark.parametrize("arguments", [("-t", "20m"), ("--time=1:2:3:4",), ("-t",), ("--time=",)])
def test_an_unreadable_time_limit_is_rejected_by_name(arguments: tuple[str, ...]) -> None:
    with pytest.raises(ValidationError, match="sbatch_arguments"):
        _config(sbatch_arguments=arguments)


@pytest.mark.parametrize("path", ["/outside/out.json", "../out.json", "nested/../out.json"])
def test_artifact_paths_are_candidate_relative(path: str, tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    with pytest.raises(SlurmError, match="candidate-relative"):
        SlurmJobRunner(_config(), processes=_FakeConnector(), clock=ManualClock()).run(
            SlurmJobRequest(
                workspace=workspace,
                command=("true",),
                file_artifacts=(SlurmFileArtifact(path, tmp_path / "out"),),
            ),
            pause=_unpaused,
        )


def test_connector_failure_does_not_expose_output(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(connector_exit_code=23)

    with pytest.raises(SlurmError, match="exit code 23") as error:
        SlurmJobRunner(_config(), processes=connector, clock=ManualClock()).run(
            SlurmJobRequest(workspace=workspace, command=("true",)), pause=_unpaused
        )
    assert "private error" not in str(error.value)


def test_nonzero_job_skips_output_artifact_collection(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    output = tmp_path / "metrics.json"
    connector = _FakeConnector(job_exit_code=2)

    result = SlurmJobRunner(_config(), processes=connector, clock=ManualClock()).run(
        SlurmJobRequest(
            workspace=workspace,
            command=("false",),
            file_artifacts=(SlurmFileArtifact("metrics.json", output),),
        ),
        pause=_unpaused,
    )

    assert result.exit_code == 2
    assert not output.exists()


class _ProgressAnswer(_FakeConnector):
    """Answers the batch progress count with ``raw``, exactly as it arrived."""

    def __init__(self, raw: str) -> None:
        super().__init__()
        self.raw = raw

    def _execute(self, framed: str) -> str:
        if "wc -l" in framed:
            return self.raw
        return super()._execute(framed)


@pytest.mark.parametrize(
    "raw",
    ["", "\n", "2\n", "2", f"2\n{SCHEDULER_READ_END}", f"{SCHEDULER_READ_END}\n"],
)
def test_a_lost_or_cut_progress_count_is_unknown_not_zero(tmp_path: Path, raw: str) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    clock = ManualClock()
    runner = SlurmJobRunner(
        _config(),
        processes=_ProgressAnswer(raw),
        invocation_id=lambda: "batch_01",
        clock=clock,
    )
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="s", command=("true",)),)
        )
    )

    with pytest.raises(SlurmError):
        runner.completed_stages(handle)


def test_a_whole_progress_count_is_read(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    clock = ManualClock()
    runner = SlurmJobRunner(
        _config(),
        processes=_ProgressAnswer(f"2\n{SCHEDULER_READ_END}\n"),
        invocation_id=lambda: "batch_01",
        clock=clock,
    )
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace, stages=(SlurmBatchStage(name="s", command=("true",)),)
        )
    )

    assert runner.completed_stages(handle) == 2


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
    clock = ManualClock()
    runner = SlurmJobRunner(
        _config(),
        processes=connector,
        invocation_id=lambda: "batch_01",
        clock=clock,
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
    wait_result = runner.wait_batch(handle, pause=clock.advance)
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
    assert 'http://127.0.0.1:"${PORT}"' in script
    assert script.index("accuracy.py") < script.index("benchmark.py") < script.index("profile.py")
    assert "vs_timeout --signal=TERM --kill-after=5s 13s python benchmark.py" in script
    assert "exit 0" in script

    handle_document = handle.model_dump(mode="json")
    handle_document["stages"][0]["file_artifacts"][0]["remote_path"] = "../../outside"
    with pytest.raises(ValidationError):
        SlurmBatchHandle.model_validate(handle_document)


@pytest.mark.parametrize("allocation_exit", [0, 1, 70, 255])
@pytest.mark.parametrize("stage_exit", [None, "", "invalid", "-1", "256", "0", "7", "SKIPPED"])
def test_batch_collection_preserves_evidence_without_inventing_outcomes(
    tmp_path: Path, allocation_exit: int, stage_exit: str | None
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    metrics = tmp_path / "metrics.json"
    accuracy = {
        "exit-code.txt": "0",
        "stdout.txt": "accuracy passed",
        "stderr.txt": "accuracy diagnostic",
        "elapsed-seconds.txt": "2",
    }
    benchmark = {
        "stdout.txt": "benchmark evidence",
        "stderr.txt": "benchmark diagnostic",
        "elapsed-seconds.txt": "3",
    }
    if stage_exit is not None:
        benchmark["exit-code.txt"] = stage_exit
    connector = _FakeConnector(
        job_exit_code=allocation_exit,
        batch_stage_results={
            "phases": {"setup-seconds.txt": "0", "service-startup-seconds.txt": "0"},
            "0000": accuracy,
            "0001": benchmark,
        },
    )
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="accuracy",
                    command=("accuracy",),
                    file_artifacts=(SlurmFileArtifact("metrics.json", metrics),),
                ),
                SlurmBatchStage(name="benchmark", command=("benchmark",)),
            ),
        )
    )

    result = runner.collect_batch(handle)

    assert result.job_exit_code == allocation_exit
    assert result.stages[0].exit_code == 0
    assert result.stages[0].stdout == "accuracy passed\n"
    assert result.stages[0].stderr == "accuracy diagnostic\n"
    assert result.stages[0].artifacts[0].local_path == metrics
    assert metrics.exists()
    assert result.stages[1].stdout == "benchmark evidence\n"
    assert result.stages[1].stderr == "benchmark diagnostic\n"
    if stage_exit in {"0", "7", "SKIPPED"}:
        assert result.stages[1].collection_failure is None
        assert result.stages[1].exit_code == (None if stage_exit == "SKIPPED" else int(stage_exit))
    else:
        assert result.stages[1].exit_code is None
        assert result.stages[1].collection_failure is not None
        assert "exit-code.txt" in result.stages[1].collection_failure
        assert not result.stages[1].skipped


@pytest.mark.parametrize("allocation_exit", [0, 70])
@pytest.mark.parametrize("stage_count", [0, 1, 2])
def test_batch_collection_only_recovers_stages_with_available_evidence(
    tmp_path: Path, allocation_exit: int, stage_count: int
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(
        job_exit_code=allocation_exit,
        batch_stage_results={
            f"{index:04d}": {
                "exit-code.txt": "0",
                "stdout.txt": f"evidence {index}",
                "stderr.txt": "",
                "elapsed-seconds.txt": "2",
            }
            for index in range(stage_count)
        },
    )
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=tuple(
                SlurmBatchStage(name=name, command=(name,)) for name in ("accuracy", "benchmark")
            ),
        )
    )

    result = runner.collect_batch(handle)

    assert len(result.stages) == (stage_count if allocation_exit else 2)
    assert all(stage.exit_code == 0 for stage in result.stages[:stage_count])
    assert all(stage.exit_code is None for stage in result.stages[stage_count:])
    assert [stage.stdout for stage in result.stages[:stage_count]] == [
        f"evidence {index}\n" for index in range(stage_count)
    ]


@pytest.mark.parametrize("allocation_exit", [0, 70])
@pytest.mark.parametrize("missing", ["stdout.txt", "stderr.txt", "elapsed-seconds.txt", "phases"])
def test_batch_collection_keeps_available_evidence_when_metadata_is_missing(
    tmp_path: Path, allocation_exit: int, missing: str
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    stage_files: dict[str, str] = {
        "exit-code.txt": "0",
        "stdout.txt": "saved stdout",
        "stderr.txt": "saved stderr",
        "elapsed-seconds.txt": "2",
    }
    stage_files.pop(missing, None)
    connector = _FakeConnector(
        job_exit_code=allocation_exit, batch_stage_results={"0000": stage_files}
    )
    metrics = tmp_path / "metrics.json"
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="accuracy",
                    command=("accuracy",),
                    file_artifacts=(SlurmFileArtifact("metrics.json", metrics),),
                ),
            ),
        )
    )

    result = runner.collect_batch(handle)

    assert result.stages[0].exit_code == 0
    assert result.stages[0].artifacts[0].local_path == metrics
    assert metrics.exists()
    assert result.stages[0].stdout == ("" if missing == "stdout.txt" else "saved stdout\n")
    assert result.stages[0].stderr == ("" if missing == "stderr.txt" else "saved stderr\n")
    if missing != "phases":
        assert result.stages[0].collection_failure is not None
    if allocation_exit == 0:
        assert result.collection_failure is not None


@pytest.mark.parametrize("allocation_exit", [0, 70])
@pytest.mark.parametrize("failed_get", [".vibesys-slurm-results", "metrics.json"])
@pytest.mark.parametrize("absent", [True, False], ids=["absent", "cut"])
def test_batch_collection_transport_failures_never_publish_successful_evidence(
    tmp_path: Path, allocation_exit: int, failed_get: str, *, absent: bool
) -> None:
    """A failed download is a missing result only when the cluster says the path is absent.

    Otherwise the download was cut short: collection reads nothing from it and raises.
    """
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    connector = _FakeConnector(
        job_exit_code=allocation_exit,
        batch_stage_results={
            "phases": {"setup-seconds.txt": "0", "service-startup-seconds.txt": "0"},
            "0000": {
                "exit-code.txt": "0",
                "stdout.txt": "saved evidence",
                "stderr.txt": "",
                "elapsed-seconds.txt": "2",
            },
        },
    )
    connector.failed_get_suffix = failed_get
    connector.failed_get_absent = absent
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(
                    name="accuracy",
                    command=("accuracy",),
                    file_artifacts=(SlurmFileArtifact("metrics.json", tmp_path / "metrics.json"),),
                ),
            ),
        )
    )

    if not absent:
        with pytest.raises(SlurmTransferInterruptedError, match=failed_get):
            runner.collect_batch(handle)
        return
    result = runner.collect_batch(handle)

    if failed_get == "metrics.json":
        assert result.stages[0].stdout == "saved evidence\n"
        assert result.stages[0].collection_failure is not None
        assert not result.stages[0].artifacts
    else:
        assert result.collection_failure is not None
        assert all(stage.exit_code is None for stage in result.stages)
        if allocation_exit:
            assert not result.stages


@pytest.mark.parametrize("allocation_exit", [0, 70])
def test_batch_collection_keeps_stage_evidence_when_local_artifact_path_is_blocked(
    tmp_path: Path, allocation_exit: int
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    blocked = tmp_path / "blocked"
    blocked.write_text("a file cannot be an artifact's parent directory")
    connector = _FakeConnector(
        job_exit_code=allocation_exit,
        batch_stage_results={
            "phases": {"setup-seconds.txt": "0", "service-startup-seconds.txt": "0"},
            **{
                f"{index:04d}": {
                    "exit-code.txt": "0",
                    "stdout.txt": f"stage {index} evidence",
                    "stderr.txt": "",
                    "elapsed-seconds.txt": "2",
                }
                for index in range(2)
            },
        },
    )
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    handle = runner.submit_batch(
        SlurmBatchRequest(
            workspace=workspace,
            stages=(
                SlurmBatchStage(name="accuracy", command=("accuracy",)),
                SlurmBatchStage(
                    name="benchmark",
                    command=("benchmark",),
                    file_artifacts=(SlurmFileArtifact("metrics.json", blocked / "metrics.json"),),
                ),
            ),
        )
    )

    result = runner.collect_batch(handle)

    assert result.stages[0].stdout == "stage 0 evidence\n"
    assert result.stages[0].exit_code == 0
    assert result.stages[0].collection_failure is None
    assert result.stages[1].stdout == "stage 1 evidence\n"
    assert result.stages[1].exit_code == 0
    assert result.stages[1].collection_failure is not None
    assert "metrics.json" in result.stages[1].collection_failure
    assert not result.stages[1].artifacts


def _service_log_tail(tmp_path: Path, log_lines: list[str]) -> tuple[str, str | None]:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
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
    clock = ManualClock()
    runner = SlurmJobRunner(
        _config(),
        processes=connector,
        invocation_id=lambda: "batch_tail",
        clock=clock,
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

    result = runner.collect_batch(runner.wait_batch(handle, pause=clock.advance).handle)
    return result.service_log_tail, connector.uploaded_script


def test_batch_result_carries_the_service_log_tail_with_repeats_collapsed(
    tmp_path: Path,
) -> None:
    warnings = ["warning: cache record A", "warning: cache record B"]
    progress = [f"decode: {step} steps, 120 ms/step" for step in range(0, 4000, 100)]
    log_lines = [line for step in progress for line in (*warnings, step)] + warnings

    service_log_tail, script = _service_log_tail(tmp_path, log_lines)

    tail = service_log_tail.splitlines()
    assert len(tail) == 40
    assert len(set(tail)) == len(tail)
    assert tail[-2:] == warnings
    assert tail[:-2] == progress[-38:]
    assert script is not None
    assert "tail -n 400 .vs-slurm-service.log > " in script


def test_service_log_tail_bounds_line_length_and_total_size(tmp_path: Path) -> None:
    log_lines = [f"{index}: " + "x" * 10_000 for index in range(40)]

    service_log_tail, _script = _service_log_tail(tmp_path, log_lines)

    tail = service_log_tail.splitlines()
    assert tail
    assert tail[-1].startswith("39: ")
    assert all(len(line) <= 450 for line in tail)
    assert len(service_log_tail) <= 8_000


def test_batch_rejects_duplicate_or_unsafe_stage_names(tmp_path: Path) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    runner = SlurmJobRunner(_config(), processes=_FakeConnector(), clock=ManualClock())

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
        processes=connector,
        invocation_id=lambda: next(invocation_ids),
        clock=ManualClock(),
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
    publish_commands = [command for command in commands if _moves(shlex.split(command))]
    assert len(publish_commands) == 2
    assert all(command.index("touch ") < command.index("mv") for command in publish_commands)

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


def test_input_that_changes_during_staging_is_reported_as_a_transient_condition(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    edited = workspace / "engine.py"
    edited.write_text("v1\n", encoding="utf-8")

    class _EditingConnector(_FilesystemConnector):
        def run(
            self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink
        ) -> ProcessRun:
            del output
            stdin = spec.input.decode() or None
            assert stdin is not None
            if json.loads(stdin)["operation"] == "sync_to":
                edited.write_text("v2\n", encoding="utf-8")
            return super().run(spec, timeout=timeout, output=DISCARD)

    runner = SlurmJobRunner(
        _config(remote_workspace_root=str(tmp_path / "remote")),
        processes=_EditingConnector(),
        invocation_id=lambda: "racing_edit",
        clock=ManualClock(),
    )

    with pytest.raises(SlurmError, match="transient infrastructure condition") as raised:
        runner.submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    assert "not a candidate failure" in str(raised.value)


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
        processes=_FilesystemConnector(),
        invocation_id=lambda: "safe_destination",
        clock=ManualClock(),
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
        processes=_FilesystemConnector(),
        invocation_id=lambda: "safe_parent",
        clock=ManualClock(),
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
        SlurmJobRunner(
            _config(),
            processes=connector,
            invocation_id=lambda: "failed_upload",
            clock=ManualClock(),
        ).submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    remote_commands = [
        str(request.get("command", ""))
        for request in connector.requests
        if request["operation"] == "exec"
    ]
    assert not any(_moves(shlex.split(command)) for command in remote_commands)
    assert not connector.ready_content_objects
    assert not connector.locked_content_objects


def test_concurrent_content_publisher_wins_without_loser_deleting_its_object(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "candidate"
    workspace.mkdir()
    (workspace / "candidate.py").write_text("same snapshot\n", encoding="utf-8")
    connector = _FakeConnector(content_cache_behavior="concurrent-publisher")

    handle = SlurmJobRunner(
        _config(), processes=connector, invocation_id=lambda: "race_01", clock=ManualClock()
    ).submit(SlurmJobRequest(workspace=workspace, command=("true",)))

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
        SlurmJobRunner(
            _config(), processes=connector, invocation_id=lambda: "busy_01", clock=ManualClock()
        ).submit(SlurmJobRequest(workspace=workspace, command=("true",)))

    publish = next(
        str(request["command"])
        for request in connector.requests
        if request["operation"] == "exec" and "for attempt in" in str(request["command"])
    )
    assert "owner=%s\\npid=%s\\ncreated=%s" in publish
    assert "[ $((now - created_at)) -ge 300 ]" in publish
    assert _moves(shlex.split(publish))
    assert "rm -rf --" in publish


class _SubmissionFaultConnector(_FakeConnector):
    """Lose transport at a chosen public submission phase."""

    def __init__(self, *, during_sbatch: bool) -> None:
        super().__init__()
        self.during_sbatch = during_sbatch
        self.scheduler_called = False

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        stdin = spec.input.decode() or None
        assert stdin is not None
        request = json.loads(stdin)
        is_sbatch = request["operation"] == "exec" and "sbatch-rr " in request["command"]
        if is_sbatch:
            self.scheduler_called = True
        if (self.during_sbatch and is_sbatch) or (
            not self.during_sbatch and request["operation"] == "exec"
        ):
            response = {"version": 1, "returncode": 255, "stdout": "", "stderr": "private"}
            return ProcessOutcome(0, json.dumps(response).encode(), b"")
        return super().run(spec, timeout=timeout, output=DISCARD)


@pytest.mark.parametrize("during_sbatch", [False, True])
def test_submission_fault_distinguishes_staging_rejection_from_unknown_scheduler_acceptance(
    tmp_path: Path, *, during_sbatch: bool
) -> None:
    """Only a fault before invoking sbatch proves no scheduler-owned resource exists."""
    process = _SubmissionFaultConnector(during_sbatch=during_sbatch)
    runner = SlurmJobRunner(_config(), processes=process, clock=ManualClock())
    with pytest.raises(SlurmError, match="transport exec failed") as raised:
        runner.submit(SlurmJobRequest(workspace=tmp_path, command=("true",)))
    assert process.scheduler_called is during_sbatch
    assert isinstance(raised.value, SlurmSubmissionRejectedError) is (not during_sbatch)


class _SubmissionFaultSshProcess(_FakeSshProcess):
    """Fail each transport phase without invoking the scheduler prematurely."""

    def __init__(self, phase: str) -> None:
        super().__init__()
        self.phase = phase
        self.scheduler_called = False

    def run(self, spec: ProcessSpec, *, timeout: float | None, output: OutputSink) -> ProcessRun:
        del output
        argv = spec.argv
        command = argv[-1]
        is_sbatch = argv[0] == "ssh" and "sbatch-rr " in command
        self.scheduler_called |= is_sbatch
        should_fail = {
            "mkdir": argv[0] == "ssh" and command.startswith("mkdir -p"),
            "staging": argv[0] == "ssh" and "printf 'READY'" in command,
            "rsync": argv[0] == "rsync" and not command.endswith("run.sbatch"),
            "script-upload": argv[0] == "rsync" and command.endswith("run.sbatch"),
            "sbatch": is_sbatch,
        }[self.phase]
        if should_fail:
            return ProcessOutcome(255, b"", b"private SSH failure")
        return super().run(spec, timeout=timeout, output=DISCARD)


@pytest.mark.parametrize("phase", ["mkdir", "staging", "rsync", "script-upload", "sbatch"])
def test_ssh_submission_failure_preserves_the_acceptance_boundary(
    tmp_path: Path, phase: str
) -> None:
    process = _SubmissionFaultSshProcess(phase)
    runner = SlurmJobRunner(
        _config(transport={"kind": "ssh", "host": "cluster.example"}),
        processes=process,
        clock=ManualClock(),
    )
    with pytest.raises(SlurmError) as raised:
        runner.submit(SlurmJobRequest(workspace=tmp_path, command=("true",)))
    assert process.scheduler_called is (phase == "sbatch")
    assert isinstance(raised.value, SlurmSubmissionRejectedError) is (phase != "sbatch")


def test_local_script_staging_failure_is_definitely_rejected(tmp_path: Path) -> None:
    scratch_root = tmp_path / "scratch-file"
    scratch_root.write_text("not a directory", encoding="utf-8")
    connector = _FakeConnector()
    runner = SlurmJobRunner(
        _config(), processes=connector, scratch_root=scratch_root, clock=ManualClock()
    )
    with pytest.raises(SlurmSubmissionRejectedError):
        runner.submit(SlurmJobRequest(workspace=tmp_path, command=("true",)))
    assert not any("sbatch-rr " in str(request.get("command")) for request in connector.requests)


def test_invalid_batch_is_definitely_rejected_before_any_transport(tmp_path: Path) -> None:
    connector = _FakeConnector()
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    with pytest.raises(SlurmSubmissionRejectedError):
        runner.submit_batch(SlurmBatchRequest(workspace=tmp_path, stages=()))
    assert connector.requests == []


def test_unencodable_job_script_is_definitely_rejected_before_sbatch(tmp_path: Path) -> None:
    connector = _FakeConnector()
    runner = SlurmJobRunner(_config(), processes=connector, clock=ManualClock())
    with pytest.raises(SlurmSubmissionRejectedError) as caught:
        runner.submit(
            SlurmJobRequest(workspace=tmp_path, command=("true",), setup_script="/operator/\ud800")
        )
    assert isinstance(caught.value.__cause__, UnicodeEncodeError)
    assert not any("sbatch-rr " in str(request.get("command")) for request in connector.requests)
