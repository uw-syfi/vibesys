"""CLI contract tests for the trusted Slurm gate wrapper."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING, TypedDict

import pytest

from vs_sandbox.api.slurm import SlurmEvaluationPlan, write_slurm_evaluation_plan

# test-isolation: main is the CLI entry point and is intentionally absent from the library API.
from vs_sandbox.slurm_command import main
from vs_slurm.api import (
    ClusterObservation,
    ClusterSubmitted,
    SlurmError,
    SlurmJobRequest,
    SlurmJobRunner,
    SlurmJobStatus,
    load_slurm_config,
)
from vs_slurm.fake_connector import HOLD_FILE, SUBMITTED_FILE, executing_cluster, recorded_commands

# test-isolation: public wiring composes the real Cluster over the executable transport Fake.
from vs_slurm.wiring import SlurmCluster

if TYPE_CHECKING:
    from pathlib import Path


class _PlanOptions(TypedDict, total=False):
    accuracy_command: tuple[str, ...]
    benchmark_command: tuple[str, ...]
    benchmark_output_argument: str


def _write_plan(
    tmp_path: Path,
    *,
    accuracy_command: tuple[str, ...] | None = None,
    benchmark_command: tuple[str, ...] | None = None,
    benchmark_output_argument: str | None = None,
) -> Path:
    config_path = tmp_path / "slurm.toml"
    config_path.write_text("[slurm]\n", encoding="utf-8")
    plan_path = tmp_path / "evaluation-plan.json"
    write_slurm_evaluation_plan(
        plan_path,
        SlurmEvaluationPlan(
            config_path=config_path,
            accuracy_command=accuracy_command,
            benchmark_command=benchmark_command,
            benchmark_output_argument=benchmark_output_argument,
        ),
    )
    return plan_path


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            ("accuracy", (), {}, "invalid accuracy invocation"),
            id="accuracy-not-configured",
        ),
        pytest.param(
            (
                "accuracy",
                ("unexpected",),
                {"accuracy_command": ("check-accuracy",)},
                "invalid accuracy invocation",
            ),
            id="accuracy-rejects-arguments",
        ),
        pytest.param(
            ("benchmark", (), {}, "benchmark is not configured"),
            id="benchmark-not-configured",
        ),
        pytest.param(
            (
                "benchmark",
                ("unexpected",),
                {"benchmark_command": ("run-benchmark",)},
                "benchmark output arguments do not match the plan",
            ),
            id="benchmark-without-output-rejects-arguments",
        ),
        pytest.param(
            (
                "benchmark",
                ("--output",),
                {
                    "benchmark_command": ("run-benchmark",),
                    "benchmark_output_argument": "--output",
                },
                "benchmark output arguments do not match the plan",
            ),
            id="benchmark-requires-output-value",
        ),
        pytest.param(
            (
                "benchmark",
                ("--output", "result.json"),
                {
                    "benchmark_command": ("run-benchmark",),
                    "benchmark_output_argument": "--output",
                },
                "benchmark output path is outside the framework namespace",
            ),
            id="benchmark-restricts-output-path",
        ),
        *(
            pytest.param(
                (
                    "benchmark",
                    ("--output", path),
                    {
                        "benchmark_command": ("run-benchmark",),
                        "benchmark_output_argument": "--output",
                    },
                    "benchmark output path is outside the framework namespace",
                ),
                id=f"benchmark-rejects-{path}",
            )
            for path in (
                "/tmp/result.json",  # noqa: S108  # lint-waiver: LW-147672 [S108]; rejected path under test.
                "/etc/vibesys-framework-benchmark-0.json",
                "/tmp/x/vibesys-framework-benchmark-0.json",  # noqa: S108  # lint-waiver: LW-954368 [S108]; rejected path under test.
                "/tmp/vibesys-framework-benchmark-../x.json",  # noqa: S108  # lint-waiver: LW-805374 [S108]; rejected path under test.
            )
        ),
    ],
)
def test_cli_rejects_invocations_that_differ_from_the_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, tuple[str, ...], _PlanOptions, str],
) -> None:
    kind, arguments, plan_options, message = case
    plan_path = _write_plan(tmp_path, **plan_options)

    exit_code = main(("--plan", str(plan_path), kind, *arguments))

    assert exit_code == 1
    assert capsys.readouterr().err == f"Slurm evaluator failed: {message}\n"


@pytest.mark.parametrize(
    ("kind", "plan_options"),
    [
        pytest.param(
            "accuracy",
            {"accuracy_command": ("check-accuracy",)},
            id="accuracy",
        ),
        pytest.param(
            "benchmark",
            {"benchmark_command": ("run-benchmark",)},
            id="benchmark",
        ),
        pytest.param(
            "benchmark",
            {
                "benchmark_command": ("run-benchmark",),
                "benchmark_output_argument": "--output",
            },
            id="benchmark-output-artifact",
        ),
    ],
)
def test_cli_advances_valid_gate_invocations_to_operator_config_validation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    plan_options: _PlanOptions,
) -> None:
    plan_path = _write_plan(tmp_path, **plan_options)
    arguments = (
        ("--output", ".vibesys-benchmark-result.json")
        if plan_options.get("benchmark_output_argument") is not None
        else ()
    )

    exit_code = main(("--plan", str(plan_path), kind, *arguments))

    assert exit_code == 1
    assert "invalid settings: name, remote_workspace_root, transport" in capsys.readouterr().err


def test_cli_accepts_the_framework_benchmark_transport_path(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # The trusted framework benchmark writes its result to this fixed /tmp
    # transport path (one nonce per run); the Slurm gate must accept it.
    plan_path = _write_plan(
        tmp_path,
        benchmark_command=("run-benchmark",),
        benchmark_output_argument="--output",
    )

    exit_code = main(
        (
            "--plan",
            str(plan_path),
            "benchmark",
            "--output",
            "/tmp/vibesys-framework-benchmark-0123456789abcdef0123456789abcdef.json",  # noqa: S108  # lint-waiver: LW-728881 [S108]; the fixed framework transport path under test.
        )
    )

    assert exit_code == 1
    assert "invalid settings: name, remote_workspace_root, transport" in capsys.readouterr().err


def test_cli_requires_explicit_writable_cluster_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state = executing_cluster(tmp_path / "cluster")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    plan_path = _write_plan(tmp_path, benchmark_command=("true",))
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(state)])
    (tmp_path / "slurm.toml").write_text(
        '[slurm]\nname = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(tmp_path / 'remote'))}\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )

    assert main(("--plan", str(plan_path), "benchmark")) == 1
    assert (
        capsys.readouterr().err
        == "Slurm evaluator failed: Slurm evaluation plan requires cluster_state_root\n"
    )
    assert recorded_commands(state) == []


def test_sigterm_cancels_the_submitted_slurm_job(tmp_path: Path) -> None:
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    os.mkfifo(state / SUBMITTED_FILE)
    config_path = tmp_path / "slurm.toml"
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(state)])
    # A one-hour poll interval: only a prompt cancellation can reach scancel.
    config_path.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(tmp_path / 'remote'))}\n"
        "poll_interval_seconds = 3600.0\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )
    plan_path = tmp_path / "evaluation-plan.json"
    write_slurm_evaluation_plan(
        plan_path,
        SlurmEvaluationPlan(
            config_path=config_path,
            cluster_state_root=tmp_path / "cluster-state",
            benchmark_command=("run-benchmark",),
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    gate = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-731005 [S603]; the test runs the real gate CLI with a fixed argv.
        # > Calling main() in-process cannot receive a real SIGTERM without
        # > signalling the test runner itself.
        [sys.executable, "-m", "vs_sandbox.slurm_command", "--plan", str(plan_path), "benchmark"],
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    job_id = (state / SUBMITTED_FILE).read_text(encoding="utf-8")
    assert job_id.isdigit()

    gate.send_signal(signal.SIGTERM)
    _, stderr = gate.communicate()

    assert gate.returncode == 128 + signal.SIGTERM, stderr
    commands = recorded_commands(state)
    assert commands.count(f"scancel {job_id}") == 1
    assert commands[-1] == f"sacct -n -X -j {job_id} --format=State,ExitCode"


def test_an_in_process_sigterm_cancels_the_job_and_reports_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    os.mkfifo(state / SUBMITTED_FILE)
    config_path = tmp_path / "slurm.toml"
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(state)])
    # A one-hour poll interval: only a prompt cancellation can reach scancel.
    config_path.write_text(
        "[slurm]\n"
        'name = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(tmp_path / 'remote'))}\n"
        "poll_interval_seconds = 3600.0\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )
    plan_path = tmp_path / "evaluation-plan.json"
    write_slurm_evaluation_plan(
        plan_path,
        SlurmEvaluationPlan(
            config_path=config_path,
            cluster_state_root=tmp_path / "cluster-state",
            benchmark_command=("run-benchmark",),
        ),
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    main_thread = threading.main_thread().ident
    assert main_thread is not None

    submitted_ids: list[str] = []

    def terminate_once_submitted() -> None:
        submitted_ids.append((state / SUBMITTED_FILE).read_text(encoding="utf-8"))
        # main() has its SIGTERM handler installed until its gate finishes.
        signal.pthread_kill(main_thread, signal.SIGTERM)

    signaller = threading.Thread(target=terminate_once_submitted)
    signaller.start()
    exit_code = main(("--plan", str(plan_path), "benchmark"))
    signaller.join()

    assert exit_code == 128 + signal.SIGTERM
    job_id = submitted_ids[0]
    assert job_id.isdigit()
    commands = recorded_commands(state)
    assert commands.count(f"scancel {job_id}") == 1
    assert commands[-1] == f"sacct -n -X -j {job_id} --format=State,ExitCode"
    assert f"Slurm evaluator cancelled: Slurm job {job_id} was cancelled" in capsys.readouterr().err


def test_cli_cancels_accepted_job_when_acceptance_publication_reply_is_lost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    wrapper = tmp_path / "lost-publication.py"
    wrapper.write_text(
        "import json, sys\nfrom pathlib import Path\n"
        "from vs_slurm.fake_connector import handle\n"
        "request = json.loads(sys.stdin.read())\n"
        "reply = handle(Path(sys.argv[1]), request)\n"
        "command = request.get('command', '')\n"
        "lost = request.get('operation') == 'exec' and 'accepted.json.pending.' in command\n"
        "sys.stdout.write('malformed reply' if lost else json.dumps(reply))\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "slurm.toml"
    connector = json.dumps([sys.executable, str(wrapper), str(state)])
    config_path.write_text(
        '[slurm]\nname = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(tmp_path / 'remote'))}\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    plan = tmp_path / "plan.json"
    write_slurm_evaluation_plan(
        plan,
        SlurmEvaluationPlan(
            config_path=config_path,
            cluster_state_root=tmp_path / "state",
            accuracy_command=("true",),
        ),
    )
    with pytest.raises(SlurmError, match="outcome is unknown"):
        main(("--plan", str(plan), "--operation-id", "lost-publication", "accuracy"))
    commands = recorded_commands(state)
    assert sum("sbatch" in command for command in commands) == 1
    assert sum(command.startswith("scancel ") for command in commands) == 1


def test_conflicting_gate_request_does_not_cancel_original_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = executing_cluster(tmp_path / "cluster")
    (state / HOLD_FILE).touch()
    config_path = tmp_path / "slurm.toml"
    connector = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(state)])
    config_path.write_text(
        '[slurm]\nname = "fake"\n'
        f"remote_workspace_root = {json.dumps(str(tmp_path / 'remote'))}\n"
        f'transport = {{ kind = "connector", command = {connector} }}\n',
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    config = load_slurm_config(config_path)
    root = tmp_path / "state"
    cluster = SlurmCluster(SlurmJobRunner(config, scratch_root=root), state_root=root)
    assert isinstance(
        cluster.submit(
            SlurmJobRequest(workspace=workspace, command=("true",)), operation_id="original"
        ),
        ClusterSubmitted,
    )
    plan = tmp_path / "plan.json"
    write_slurm_evaluation_plan(
        plan,
        SlurmEvaluationPlan(
            config_path=config_path, cluster_state_root=root, accuracy_command=("false",)
        ),
    )
    with pytest.raises(SlurmError, match="another payload"):
        main(("--plan", str(plan), "--operation-id", "original", "accuracy"))
    observed = cluster.inspect("original")
    assert isinstance(observed, ClusterObservation)
    assert observed.status is SlurmJobStatus.PENDING
    assert not any(command.startswith("scancel ") for command in recorded_commands(state))
