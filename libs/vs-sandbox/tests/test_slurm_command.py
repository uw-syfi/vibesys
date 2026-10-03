"""CLI contract tests for the trusted Slurm gate wrapper."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

import pytest

from vs_sandbox.api.slurm import SlurmEvaluationPlan, write_slurm_evaluation_plan

# test-isolation: main is the CLI entry point and is intentionally absent from the library API.
from vs_sandbox.slurm_command import main

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
