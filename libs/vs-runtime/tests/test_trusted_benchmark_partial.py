"""A failed protocol benchmark's partial measurement reaches the trusted result."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_runtime.api import PartialMeasurement, Progress
from vs_runtime.api.infrastructure import (
    ProtocolBenchmarkContract,
    ScalarBenchmarkContract,
    build_trusted_benchmark_command,
    decode_trusted_benchmark_partial,
)

if TYPE_CHECKING:
    from pathlib import Path

_PARTIAL = PartialMeasurement(
    name="warmup_output_tokens_per_s",
    value=16.4,
    direction="max",
    unit="output tokens/s",
    target=79.7,
    progress=Progress(completed=15, required=72, unit="rounds"),
)
_HELLO = {"kind": "hello", "protocol": 2, "metrics": {"tok_s": {"direction": "max"}}}


def _evaluator(tmp_path: Path, records: list[dict[str, object]], exit_code: int) -> str:
    """Write an evaluator that writes `records` to its `--vs-output` and exits."""
    script = tmp_path / "bench.py"
    script.write_text(
        "import json, pathlib, sys\n"
        "path = pathlib.Path(sys.argv[sys.argv.index('--vs-output') + 1])\n"
        f"path.write_text(''.join(json.dumps(r) + '\\n' for r in {records!r}))\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return shlex.join([sys.executable, str(script)])


def _run(command: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    framed = build_trusted_benchmark_command(
        command, ProtocolBenchmarkContract(), str(tmp_path / "result.jsonl")
    )
    # > A FakeSandbox would not run bash, so it could not show that the framing keeps
    # > the benchmark's exit status; shell=False is already in effect.
    return subprocess.run(  # noqa: S603  # lint-waiver: LW-118205 [S603]; the command is this test's own evaluator script framed by the code under test; running it in a real shell is what the test checks.
        ["/bin/bash", "-c", framed],
        capture_output=True,
        text=True,
        check=False,
    )


@settings(deadline=None, max_examples=10)
@given(exit_code=st.integers(min_value=1, max_value=120))
def test_a_failed_benchmark_keeps_its_exit_status_and_frames_its_error_record(
    tmp_path_factory: pytest.TempPathFactory, exit_code: int
) -> None:
    tmp_path = tmp_path_factory.mktemp("bench")
    error = {"kind": "error", "message": "warmup stopped", "partial": _PARTIAL.model_dump()}
    command = _evaluator(tmp_path, [_HELLO, error], exit_code)

    completed = _run(command, tmp_path)

    assert completed.returncode == exit_code
    assert (
        decode_trusted_benchmark_partial(completed.stdout, ProtocolBenchmarkContract()) == _PARTIAL
    )


def test_an_error_without_a_partial_measurement_reports_none(tmp_path: Path) -> None:
    command = _evaluator(tmp_path, [_HELLO, {"kind": "error", "message": "crashed"}], 1)

    completed = _run(command, tmp_path)

    assert decode_trusted_benchmark_partial(completed.stdout, ProtocolBenchmarkContract()) is None


@pytest.mark.parametrize(
    "records",
    [[], [_HELLO], [_HELLO, {"kind": "result", "values": {"tok_s": 1.0}}]],
    ids=["no-file-content", "killed-after-hello", "result-then-nonzero-exit"],
)
def test_a_run_without_an_error_record_reports_no_partial_measurement(
    tmp_path: Path, records: list[dict[str, object]]
) -> None:
    completed = _run(_evaluator(tmp_path, records, 3), tmp_path)

    assert completed.returncode == 3
    assert decode_trusted_benchmark_partial(completed.stdout, ProtocolBenchmarkContract()) is None


def test_a_malformed_partial_measurement_names_the_offending_key(tmp_path: Path) -> None:
    partial = {**_PARTIAL.model_dump(), "eta_s": 4}
    command = _evaluator(
        tmp_path, [_HELLO, {"kind": "error", "message": "stop", "partial": partial}], 1
    )

    completed = _run(command, tmp_path)

    with pytest.raises(ValueError, match=r"UNKNOWN_KEY.*partial\.eta_s"):
        decode_trusted_benchmark_partial(completed.stdout, ProtocolBenchmarkContract())


def test_a_passing_benchmark_still_exits_zero_with_its_result_framed(tmp_path: Path) -> None:
    command = _evaluator(tmp_path, [_HELLO, {"kind": "result", "values": {"tok_s": 2.5}}], 0)

    completed = _run(command, tmp_path)

    assert completed.returncode == 0
    assert '"tok_s": 2.5' in completed.stdout


def test_a_scalar_contract_has_no_partial_measurement() -> None:
    contract = ScalarBenchmarkContract(output_argument="--output-json", metric="tok_s")
    framed = json.dumps({"partial": _PARTIAL.model_dump()})

    assert decode_trusted_benchmark_partial(framed, contract) is None
