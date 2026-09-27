"""Execute one trusted gate through an operator configured Slurm target."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.api.slurm import (
    SlurmEvaluationPlan,
    load_slurm_policy,
    read_slurm_evaluation_plan,
)
from vs_slurm.api import SlurmFileArtifact, SlurmJobRequest, SlurmJobRunner, load_slurm_config

if TYPE_CHECKING:
    from collections.abc import Sequence

_BENCHMARK_OUTPUT_PREFIX = ".vibesys-benchmark-"
_BENCHMARK_OUTPUT_SUFFIX = ".json"
_BENCHMARK_OUTPUT_ARGUMENT_COUNT = 2


class SlurmCommandError(ValueError):
    """A trusted gate wrapper invocation does not match its prepared plan."""

    @classmethod
    def invalid_accuracy(cls) -> SlurmCommandError:
        """Describe an accuracy invocation that differs from its plan."""
        return cls("invalid accuracy invocation")

    @classmethod
    def invalid_kind(cls, kind: str) -> SlurmCommandError:
        """Describe an unsupported gate kind."""
        return cls(f"unknown gate kind: {kind}")

    @classmethod
    def benchmark_unconfigured(cls) -> SlurmCommandError:
        """Describe a benchmark request without a planned command."""
        return cls("benchmark is not configured")

    @classmethod
    def invalid_benchmark_arguments(cls) -> SlurmCommandError:
        """Describe output arguments that differ from the trusted plan."""
        return cls("benchmark output arguments do not match the plan")

    @classmethod
    def invalid_benchmark_output(cls) -> SlurmCommandError:
        """Describe an output path outside the framework namespace."""
        return cls("benchmark output path is outside the framework namespace")


def run_gate(plan_path: Path, kind: str, arguments: Sequence[str]) -> int:
    """Run one planned accuracy or benchmark command and mirror its output."""
    plan = read_slurm_evaluation_plan(plan_path)
    policy = load_slurm_policy(plan.config_path)
    if kind == "accuracy":
        if plan.accuracy_command is None or arguments:
            raise SlurmCommandError.invalid_accuracy()
        command = (*plan.accuracy_command, *policy.accuracy_arguments)
        artifacts: tuple[SlurmFileArtifact, ...] = ()
    elif kind == "benchmark":
        command, artifacts = _benchmark_command(
            plan, arguments, extra_arguments=policy.benchmark_arguments
        )
    else:
        raise SlurmCommandError.invalid_kind(kind)
    result = SlurmJobRunner(load_slurm_config(plan.config_path)).run(
        SlurmJobRequest(
            workspace=Path.cwd(),
            command=policy.remote_argv(command),
            setup_script=policy.setup_script,
            service=policy.remote_service(),
            support_trees=plan.support_paths,
            file_artifacts=artifacts,
        )
    )
    if result.output:
        sys.stdout.write(result.output)
        if not result.output.endswith("\n"):
            sys.stdout.write("\n")
    return result.exit_code


def _benchmark_command(
    plan: SlurmEvaluationPlan,
    arguments: Sequence[str],
    *,
    extra_arguments: Sequence[str],
) -> tuple[tuple[str, ...], tuple[SlurmFileArtifact, ...]]:
    if plan.benchmark_command is None:
        raise SlurmCommandError.benchmark_unconfigured()
    output_argument = plan.benchmark_output_argument
    if output_argument is None:
        if arguments:
            raise SlurmCommandError.invalid_benchmark_arguments()
        return (*plan.benchmark_command, *extra_arguments), ()
    if len(arguments) != _BENCHMARK_OUTPUT_ARGUMENT_COUNT or arguments[0] != output_argument:
        raise SlurmCommandError.invalid_benchmark_arguments()
    local_output = arguments[1]
    if not local_output.startswith(_BENCHMARK_OUTPUT_PREFIX) or not local_output.endswith(
        _BENCHMARK_OUTPUT_SUFFIX
    ):
        raise SlurmCommandError.invalid_benchmark_output()
    remote_output = ".vibesys-framework-benchmark.json"
    return (
        (*plan.benchmark_command, *extra_arguments, output_argument, remote_output),
        (SlurmFileArtifact(remote_path=remote_output, local_path=Path(local_output)),),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and execute one planned gate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("kind", choices=("accuracy", "benchmark"))
    parsed, remainder = parser.parse_known_args(argv)
    try:
        return run_gate(parsed.plan, parsed.kind, remainder)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"Slurm evaluator failed: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
