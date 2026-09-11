"""Tests for ``vibesys.loops.gates`` -- the framework gates shared by loops.

These exercise the benchmark gate through a real ``bash`` judge backend so the
transport file it writes under ``/tmp`` genuinely exists. The gate's guarantee
is about a file on disk, so a mocked backend cannot observe it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from vibesys.input_manifest import BenchmarkResult
from vibesys.loops.gates import (
    _BENCHMARK_OUTPUT_PREFIX,
    read_protocol_benchmark,
    run_benchmark_gate,
)
from vibesys.loops.metrics import Objective

_SCALAR_SPEC = BenchmarkResult(json_argument="--out", metric="tok_per_sec")


class _ShellJudgeBackend:
    """A judge backend that really runs the gate's command line under bash."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def execute(self, command, timeout=None):  # noqa: ANN001, ANN202  # tracked: #288
        self.commands.append(command)
        proc = subprocess.run(  # noqa: S603  # tracked: #288
            ["bash", "-c", command],  # noqa: S607  # tracked: #288
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return SimpleNamespace(exit_code=proc.returncode, output=proc.stdout)


def _gate_ctx_with(backend: object, benchmark_command: str = "benchmark") -> MagicMock:
    """A loop context that runs its benchmark through *backend*."""
    ctx = MagicMock()
    ctx.judge_benchmark_command = benchmark_command
    ctx.trusted_input_changes.return_value = []
    ctx.judge_backend = backend
    return ctx


def _gate_ctx(benchmark_command: str) -> MagicMock:
    """A loop context whose benchmark command really runs."""
    return _gate_ctx_with(_ShellJudgeBackend(), benchmark_command)


def _writer_command(payload: str, tmp_path: Path, *, fail_after_write: bool = False) -> str:
    """A benchmark command that copies *payload* to the path passed last.

    The gate appends ``<output flag> <output path>`` to the configured
    benchmark command, so a writer that reads ``sys.argv[-1]`` stands in for a
    real benchmark honoring its result contract. ``fail_after_write`` writes
    the file and then exits nonzero.
    """
    source = tmp_path / "payload.json"
    source.write_text(payload)
    tail = "; sys.exit(1)" if fail_after_write else ""
    return (
        f'{sys.executable} -c "import sys, shutil; '
        f"shutil.copyfile('{source}', sys.argv[-1]){tail}\""
    )


def _transport_artifact(command: str) -> Path:
    """The result file the gate told the benchmark to write."""
    match = re.search(rf"cat ({re.escape(_BENCHMARK_OUTPUT_PREFIX)}\S+\.json)", command)
    assert match is not None
    return Path(match.group(1))


def test_benchmark_gate_fails_instead_of_reporting_a_stale_result() -> None:
    """Regression for #480: a benchmark that writes nothing cannot pass.

    The result path used to be a fixed
    ``/tmp/vibesys-framework-benchmark-<round>-<retry>.json``. A file left
    there by an earlier round, a concurrent run, or another user on the same
    host satisfied this invocation's ``cat``, and the gate reported that stale
    number as the candidate's measurement. Plant exactly such a file, then run
    a benchmark that writes no result at all: the gate must fail rather than
    hand the loop 999.0.
    """
    slug = f"stale-{os.getpid()}"
    stale = Path(f"{_BENCHMARK_OUTPUT_PREFIX}{slug}.json")
    stale.write_text('{"tok_per_sec": 999.0}')
    try:
        result = run_benchmark_gate(
            _gate_ctx("true"),
            result_spec=_SCALAR_SPEC,
            process_id="benchmark",
            output_slug=slug,
        )
    finally:
        stale.unlink(missing_ok=True)

    assert not result.passed
    assert result.outcome.feedback is not None
    assert result.outcome.metric_value is None


def test_benchmark_gate_removes_its_transport_artifact(tmp_path: Path) -> None:
    """The per-invocation result file is removed on every exit path.

    The result is recovered from stdout, so the file is dead weight once the
    command returns; leaving it behind leaks one JSON per benchmark on hosts
    where ``/tmp`` persists. Success, malformed output, and a nonzero exit
    after the file was written all leave through the same cleanup.
    """
    ctx = _gate_ctx(_writer_command('{"tok_per_sec": 42.0}', tmp_path))
    result = run_benchmark_gate(
        ctx, result_spec=_SCALAR_SPEC, process_id="benchmark", output_slug="9-2"
    )
    assert result.passed
    assert result.outcome.metric_value == 42.0
    assert not _transport_artifact(ctx.judge_backend.commands[0]).exists()

    ctx = _gate_ctx(_writer_command("this is not json", tmp_path))
    result = run_benchmark_gate(
        ctx, result_spec=_SCALAR_SPEC, process_id="benchmark", output_slug="9-2"
    )
    assert not result.passed
    assert not _transport_artifact(ctx.judge_backend.commands[0]).exists()

    ctx = _gate_ctx(_writer_command('{"tok_per_sec": 42.0}', tmp_path, fail_after_write=True))
    result = run_benchmark_gate(
        ctx, result_spec=_SCALAR_SPEC, process_id="benchmark", output_slug="9-2"
    )
    assert not result.passed
    assert not _transport_artifact(ctx.judge_backend.commands[0]).exists()


def test_a_timed_out_benchmarks_late_result_cannot_be_read_by_the_next_run() -> None:
    """The documented limit of the cleanup, and the property that survives it.

    ``DockerSandbox.execute`` reports a timeout as exit code -1 instead of
    raising, so the gate's cleanup runs while the timed-out benchmark may
    still be alive and can write its result file afterwards. That orphan
    can outlive the gate. What it must never do is
    satisfy a later invocation's ``cat``, which the per-invocation nonce
    guarantees: the next run fails rather than reporting the orphan's number.
    """
    orphans: list[Path] = []

    class _TimingOutBackend:
        """Times out the benchmark, then writes its result file too late."""

        def __init__(self) -> None:
            self.commands: list[str] = []

        def execute(self, command, timeout=None):  # noqa: ANN001, ANN202, ARG002  # tracked: #288
            self.commands.append(command)
            if "cat " not in command:  # the cleanup rm
                return SimpleNamespace(exit_code=0, output="")
            path = _transport_artifact(command)
            path.write_text('{"tok_per_sec": 999.0}')
            orphans.append(path)
            return SimpleNamespace(exit_code=-1, output="Command timed out after 5s")

    try:
        first = run_benchmark_gate(
            _gate_ctx_with(_TimingOutBackend()),
            result_spec=_SCALAR_SPEC,
            process_id="benchmark",
            output_slug="7-0",
        )
        assert not first.passed
        assert len(orphans) == 1
        assert orphans[0].exists()

        second = run_benchmark_gate(
            _gate_ctx("true"),
            result_spec=_SCALAR_SPEC,
            process_id="benchmark",
            output_slug="7-0",
        )
        assert not second.passed
        assert second.outcome.metric_value is None
    finally:
        for orphan in orphans:
            orphan.unlink(missing_ok=True)


@pytest.mark.parametrize("slug", ["", "../escape", "nested/slug"])
def test_benchmark_gate_rejects_an_output_slug_that_is_not_one_segment(slug: str) -> None:
    """A slug is interpolated into the result path, so it stays one segment."""
    with pytest.raises(ValueError, match="output_slug"):
        run_benchmark_gate(
            _gate_ctx("true"),
            result_spec=_SCALAR_SPEC,
            process_id="benchmark",
            output_slug=slug,
        )


def test_benchmark_gate_keeps_the_evaluator_declared_direction(tmp_path: Path) -> None:
    """Regression for #477: an unconfigured axis keeps the declared direction.

    The gate used to derive the direction from ``objectives.toml`` alone and
    fall back to ``max`` for the legacy scalar contract, so a protocol-2
    evaluator declaring a latency metric as ``min`` had its reading compared
    the wrong way round whenever the task configured no objectives. The
    evaluator's declaration is now the fallback, and an axis with no known
    direction stays ``None`` so ``MetricSpace`` reports ``INCOMPARABLE``
    instead of guessing.
    """
    stream = (
        '{"kind":"hello","protocol":2,'
        '"metrics":{"p99_latency_ns":{"unit":"ns","direction":"min"}}}\n'
        '{"kind":"result","values":{"p99_latency_ns":1250.0}}'
    )
    result = run_benchmark_gate(
        _gate_ctx(_writer_command(stream, tmp_path)),
        result_spec=None,
        result_protocol=2,
        objectives=(),
        process_id="benchmark",
        output_slug="4-0",
    )

    assert result.passed, result.outcome.feedback
    assert result.outcome.metric_name == "p99_latency_ns"
    assert result.outcome.metric_direction == "min"


def test_configured_objective_overrides_the_declared_direction() -> None:
    """``objectives.toml`` still wins over the evaluator's declaration."""
    stream = (
        '{"kind":"hello","protocol":2,'
        '"metrics":{"score":{"unit":"points","direction":"min"}}}\n'
        '{"kind":"result","values":{"score":3.0}}'
    )

    outcome = read_protocol_benchmark(stream, objectives=[Objective(name="score", direction="max")])

    assert outcome.feedback is None
    assert outcome.metric_direction == "max"
