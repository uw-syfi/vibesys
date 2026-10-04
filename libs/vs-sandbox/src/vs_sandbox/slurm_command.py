"""Execute one trusted gate through an operator configured Slurm target.

``SIGTERM`` or ``SIGINT`` cancels the gate: a submitted job is cancelled
(``scancel``) before the process exits with status 143. A sandbox stopping
this command therefore never leaves its job queued.
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.api.slurm import (
    SlurmEvaluationPlan,
    load_slurm_policy,
    read_slurm_evaluation_plan,
)
from vs_sandbox.slurm_wiring import make_cluster
from vs_slurm.api import (
    ClusterCollected,
    ClusterConflict,
    ClusterObservation,
    ClusterRejected,
    ClusterSubmitted,
    ClusterUnknown,
    SlurmError,
    SlurmFileArtifact,
    SlurmJobHandle,
    SlurmJobRequest,
    SlurmJobResult,
    SlurmJobStatus,
    load_slurm_config,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from types import FrameType

    from vs_slurm.api import (
        Cluster,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        ClusterSubmitOutcome,
        SlurmConfig,
    )

_BENCHMARK_OUTPUT_PREFIX = ".vibesys-benchmark-"
_BENCHMARK_OUTPUT_SUFFIX = ".json"
# The trusted framework benchmark writes to this fixed transport path with a
# hex nonce (vs_runtime._trusted_evaluation); accept exactly that shape.
_FRAMEWORK_BENCHMARK_OUTPUT = re.compile(
    r"/tmp/vibesys-framework-benchmark-[0-9a-f]+\.json"  # noqa: S108  # lint-waiver: LW-352320 [S108]; the fixed framework benchmark transport path, not a temp file.
)
_BENCHMARK_OUTPUT_ARGUMENT_COUNT = 2
_CANCELLED_EXIT_CODE = 128 + signal.SIGTERM
_CANCEL_SIGNALS = (signal.SIGTERM, signal.SIGINT)


class SlurmCommandError(ValueError):
    """A trusted gate wrapper invocation does not match its prepared plan."""

    @classmethod
    def missing_cluster_state(cls) -> SlurmCommandError:
        """Reject a plan without explicit durable cluster storage."""
        return cls("Slurm evaluation plan requires cluster_state_root")

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


def run_gate(
    plan_path: Path,
    kind: str,
    arguments: Sequence[str],
    *,
    cancel: threading.Event | None = None,
    operation_id: str | None = None,
) -> int:
    """Run one planned accuracy or benchmark command and mirror its output.

    Setting *cancel* cancels the submitted job and raises :class:`SlurmError`.
    """
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
    cancel = cancel or threading.Event()

    config = load_slurm_config(plan.config_path)
    if plan.cluster_state_root is None:
        raise SlurmCommandError.missing_cluster_state()
    cluster = make_cluster(config, state_root=plan.cluster_state_root)
    operation_id = operation_id if operation_id is not None else uuid.uuid4().hex
    submitted = cluster.submit(
        SlurmJobRequest(
            workspace=Path.cwd(),
            command=policy.remote_argv(command),
            setup_script=policy.setup_script,
            service=policy.remote_service(),
            support_trees=plan.support_paths,
            file_artifacts=artifacts,
            cancel_event=cancel,
        ),
        operation_id=operation_id,
    )
    if isinstance(submitted, (ClusterConflict, ClusterRejected)):
        raise SlurmError(submitted.reason)
    with _cancel_on_failure(cluster, operation_id):
        handle = _submitted_handle(submitted)
        _wait_for_terminal(cluster, handle, cancel, config)
        collected = cluster.collect(handle)
        return _print_collected(collected, operation_id, handle.job_id)


def _print_collected(collected: ClusterCollectOutcome, operation_id: str, job_id: str) -> int:
    if not isinstance(collected, (ClusterCollected, ClusterUnknown)) or not isinstance(
        collected.result, SlurmJobResult
    ):
        raise SlurmError.job_not_terminal(job_id)
    result = collected.result
    if result.output:
        sys.stdout.write(result.output)
        if not result.output.endswith("\n"):
            sys.stdout.write("\n")
    if isinstance(collected, ClusterUnknown):
        reason = f"Slurm operation {operation_id!r} outcome is unknown: {collected.reason}"
        raise SlurmError(reason)
    if result.exit_code is None:
        raise SlurmError.malformed_result()
    return result.exit_code


def _submitted_handle(submitted: ClusterSubmitOutcome) -> SlurmJobHandle:
    if isinstance(submitted, ClusterSubmitted) and isinstance(submitted.handle, SlurmJobHandle):
        return submitted.handle
    if isinstance(submitted, ClusterUnknown):
        reason = (
            f"Slurm operation {submitted.operation_id!r} outcome is unknown: {submitted.reason}"
        )
        raise SlurmError(reason)
    reason = (
        submitted.reason
        if not isinstance(submitted, ClusterSubmitted)
        else "unexpected batch handle"
    )
    raise SlurmError(reason)


def _wait_for_terminal(
    cluster: Cluster,
    handle: SlurmJobHandle,
    cancel: threading.Event,
    config: SlurmConfig,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    deadline = clock() + config.job_timeout_seconds
    while True:
        if cancel.is_set():
            raise SlurmError.cancelled(handle.job_id)
        if _is_terminal(cluster.inspect(handle)):
            return
        remaining = deadline - clock()
        if remaining <= 0:
            raise SlurmError.job_timed_out(handle.job_id)
        cancel.wait(min(config.poll_interval_seconds, remaining))


@contextmanager
def _cancel_on_failure(cluster: Cluster, target: str | SlurmJobHandle) -> Iterator[None]:
    identity = target if isinstance(target, str) else target.job_id
    try:
        yield
    except BaseException as error:
        try:
            cancellation = cluster.cancel(target)
            observed = cluster.inspect(target)
            if isinstance(cancellation, ClusterUnknown) or not _is_terminal(observed):
                error.add_note(
                    f"Slurm operation {identity!r} termination is unknown; inspect its operation"
                )
        except (SlurmError, OSError) as cleanup_error:
            error.add_note(f"Slurm operation {identity!r} cancellation failed: {cleanup_error}")
        raise


def _is_terminal(observed: ClusterInspectOutcome) -> bool:
    return isinstance(observed, ClusterObservation) and observed.status in {
        SlurmJobStatus.COMPLETED,
        SlurmJobStatus.FAILED,
        SlurmJobStatus.CANCELLED,
    }


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
    workspace_output = local_output.startswith(_BENCHMARK_OUTPUT_PREFIX) and local_output.endswith(
        _BENCHMARK_OUTPUT_SUFFIX
    )
    if not workspace_output and _FRAMEWORK_BENCHMARK_OUTPUT.fullmatch(local_output) is None:
        raise SlurmCommandError.invalid_benchmark_output()
    remote_output = ".vibesys-framework-benchmark.json"
    return (
        (*plan.benchmark_command, *extra_arguments, output_argument, remote_output),
        # A failed benchmark may have written why (an evaluator protocol
        # `error` record), so its result file is copied back either way.
        (
            SlurmFileArtifact(
                remote_path=remote_output,
                local_path=Path(local_output),
                collect_on_failure=True,
            ),
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse and execute one planned gate, cancelling it on SIGTERM or SIGINT."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("kind", choices=("accuracy", "benchmark"))
    parsed, remainder = parser.parse_known_args(argv)
    cancel = threading.Event()
    outcome: list[int | BaseException] = []

    def gate() -> None:
        try:
            outcome.append(
                run_gate(
                    parsed.plan,
                    parsed.kind,
                    remainder,
                    cancel=cancel,
                    operation_id=parsed.operation_id,
                )
            )
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-731003 [BLE001]; the worker hands every outcome to the main thread, which reports it.
            # > Catching narrower types would let an unexpected error vanish
            # > with the worker thread instead of failing this command.
            outcome.append(error)

    def request_cancel(_signal: int, _frame: FrameType | None) -> None:
        cancel.set()

    # The gate runs in a worker so the main thread only waits: a signal
    # handler that sets the event can then never interrupt a thread holding
    # the event's lock.
    previous = {number: signal.signal(number, request_cancel) for number in _CANCEL_SIGNALS}
    try:
        worker = threading.Thread(target=gate, name="slurm-gate")
        worker.start()
        worker.join()
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)
    result = outcome[0]
    if isinstance(result, int):
        return result
    if cancel.is_set() and isinstance(result, SlurmError):
        sys.stderr.write(f"Slurm evaluator cancelled: {_described(result)}\n")
        return _CANCELLED_EXIT_CODE
    if isinstance(result, (OSError, ValueError)):
        sys.stderr.write(f"Slurm evaluator failed: {_described(result)}\n")
        return 1
    raise result


def _described(error: BaseException) -> str:
    """Return *error* with its notes, which report a job left uncancelled."""
    return "\n".join((str(error), *getattr(error, "__notes__", ())))


if __name__ == "__main__":
    sys.exit(main())
