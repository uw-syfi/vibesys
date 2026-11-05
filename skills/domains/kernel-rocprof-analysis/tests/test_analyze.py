"""The `kernel-rocprof-analysis` skill's analysis tools, run the way an agent runs them.

The scripts are standalone programs, so their public surface is `analyze.py`'s command line
(`main(argv)`, its stdout, its exit status) plus the pure helpers a property needs. Fixtures
under `fixtures/` are small synthetic rocprofv3 outputs in the documented column layout,
written by `make_fixtures.py` beside this file.
"""

from __future__ import annotations

import csv
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(SCRIPTS))

import rocprof_compare  # noqa: E402  (the scripts directory is on sys.path only from here on)
import rocprof_trace  # noqa: E402


def _load_cli() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "kernel_rocprof_analyze_cli", SCRIPTS / "analyze.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cli = _load_cli()


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    status = cli.main([str(arg) for arg in argv])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


GEMM = "Cijk_Ailk_Bljk_BBS_BH_MT128x128x32_MI32x32x8_ISA90a_WG128_2_1"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        pytest.param(("trace", "kernels", FIXTURES / "timeline_a"), "800.00us", id="kernels"),
        pytest.param(("trace", "families", FIXTURES / "timeline_a"), "Tensile", id="families"),
        pytest.param(
            ("trace", "idle_gaps", FIXTURES / "timeline_a"), "Idle gaps found: 5", id="gaps"
        ),
        pytest.param(
            ("trace", "cpu_overhead", FIXTURES / "timeline_a"), "hipExtModuleLaunchKernel", id="cpu"
        ),
        pytest.param(
            ("trace", "memory", FIXTURES / "timeline_a"), "1 of 1 memory copies", id="memory"
        ),
        pytest.param(
            ("trace", "host_idle", FIXTURES / "timeline_a"), "trace looks valid", id="host-idle"
        ),
        pytest.param(
            ("trace", "summary", FIXTURES / "timeline_a"), "ROCPROFV3 TRACE SUMMARY", id="summary"
        ),
        pytest.param(
            ("counters", "report", FIXTURES / "pmc_a", "--arch", "gfx90a"),
            "L2",
            id="counter-report",
        ),
        pytest.param(
            ("counters", "triage", FIXTURES / "pmc_a", "--arch", "mi210"), "verdict:", id="triage"
        ),
        pytest.param(("counters", "plan", "--arch", "gfx90a", "--sets", "l2"), "--pmc", id="plan"),
        pytest.param(("att", "hotspots", FIXTURES / "att"), "attn.py:38", id="att"),
        pytest.param(
            ("compare", "timeline", FIXTURES / "timeline_a", FIXTURES / "timeline_b"),
            "+ triton_red_fused_rms_norm_1",
            id="compare-timeline",
        ),
        pytest.param(
            (
                "compare",
                "counters",
                "--arch",
                "gfx90a",
                "--a",
                FIXTURES / "pmc_a",
                "--b",
                FIXTURES / "pmc_b",
            ),
            "delta=+5.0000",
            id="compare-counters",
        ),
        pytest.param(
            ("torch", "certify", FIXTURES / "torch" / "trace.pt.trace.json"),
            "[PASS] gpu_kernels",
            id="certify",
        ),
        pytest.param(
            ("torch", "gemm-shapes", FIXTURES / "torch" / "trace.pt.trace.json"),
            "32x11008x4096",
            id="gemm",
        ),
        pytest.param(
            ("torch", "roofline", FIXTURES / "torch" / "trace.pt.trace.json"),
            "MI210",
            id="roofline",
        ),
    ],
)
def test_every_tool_reads_its_fixture(
    capsys: pytest.CaptureFixture[str], argv: tuple[object, ...], expected: str
) -> None:
    status, out, err = _run(capsys, *argv)
    assert status == 0, err
    assert expected in out


def test_the_capture_record_beside_a_trace_is_never_read_as_trace_data(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A garbage `capture.json` changes nothing: it is the wrapper's record, not profiler output."""
    without = tmp_path / "without"
    shutil.copytree(FIXTURES / "timeline_a", without)
    (without / "capture.json").unlink()
    with_garbage = tmp_path / "garbage"
    shutil.copytree(without, with_garbage)
    (with_garbage / "capture.json").write_text("{not json", encoding="utf-8")
    outputs = []
    for report in (without, with_garbage):
        status, out, err = _run(capsys, "trace", "summary", report)
        assert status == 0, err
        outputs.append(out.replace(str(report), "<report>"))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(("trace", "kernels", "{missing}"), id="trace"),
        pytest.param(("att", "hotspots", "{missing}"), id="att"),
        pytest.param(("bench", "parse", "{missing}"), id="bench"),
    ],
)
def test_a_missing_report_exits_1_naming_it(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, argv: tuple[str, ...]
) -> None:
    missing = str(tmp_path / "no-such-capture")
    status, _out, err = _run(capsys, *(arg.format(missing=missing) for arg in argv))
    assert status == cli.EXIT_UNREADABLE_INPUT
    assert "no-such-capture" in err


def test_an_unknown_window_is_an_error_not_the_whole_run(
    capsys: pytest.CaptureFixture[str],
) -> None:
    status, out, err = _run(capsys, "trace", "kernels", FIXTURES / "timeline_a", "--window", "load")
    assert status == cli.EXIT_UNREADABLE_INPUT
    assert "unknown window 'load'" in err
    assert "Kernel" not in out


def test_an_unknown_group_is_a_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    status, _out, err = _run(capsys, "flamegraph")
    assert status == cli.EXIT_USAGE
    assert "trace" in err


def test_compute_analyze_reads_the_raw_csvs_without_rocprof_compute(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without `rocprof-compute` on this machine, analyze still reports the workload's own CSVs."""
    monkeypatch.setenv("VIBESYS_ROCPROF_COMPUTE_BIN", str(tmp_path / "absent-rocprof-compute"))
    workload = tmp_path / "workload"
    workload.mkdir()
    with (workload / "pmc_kernel_top.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Kernel_Name", "Count", "Sum(ns)", "Mean(ns)", "Median(ns)", "Pct"])
        writer.writerow([GEMM, 4, 1600000, 400000, 400000, 87.5])
    status, out, err = _run(capsys, "compute", "analyze", workload)
    assert status == 0, err
    assert "falling back to raw CSVs" in out
    assert "87.5%" in out


def test_bench_parse_and_verdict_round_trip(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    log = tmp_path / "driver.log"
    log.write_text("".join(f"wall_ms: {1.0 + i / 100}\n" for i in range(5)) + "wall_ms_done: 5\n")
    status, out, err = _run(capsys, "bench", "parse", log)
    assert status == 0, err
    assert json.loads(out)["wall_ms"] == pytest.approx([1.0, 1.01, 1.02, 1.03, 1.04])
    pairs = tmp_path / "pairs.json"
    pairs.write_text(json.dumps({"pairs": [[1.0, 0.8]] * 12}))
    status, out, err = _run(capsys, "bench", "verdict", pairs)
    assert status == 0, err
    assert out.strip()


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_KERNEL_NAMES = st.sampled_from(
    [GEMM, "triton_poi_fused_add_0", "void ck_tile::kentry<1>(K)", "rocblas_gemm_kernel"]
)
_DISPATCHES = st.lists(
    st.tuples(_KERNEL_NAMES, st.integers(min_value=1, max_value=10_000), st.integers(0, 5_000)),
    min_size=1,
    max_size=30,
)


def _write_kernel_trace(
    root: Path, dispatches: list[tuple[str, int, int]]
) -> list[tuple[str, int, int]]:
    """Write one kernel trace; return (name, start_ns, duration_ns) per dispatch."""
    rows, events, t = [], [], 1_000_000
    for corr, (name, duration, gap) in enumerate(dispatches, 1):
        t += gap
        rows.append(["KERNEL_DISPATCH", "Agent 1", 1, name, corr, t, t + duration])
        events.append((name, t, duration))
        t += duration
    path = root / "host-a" / "1" / "1_kernel_trace.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "Kind",
                "Agent_Id",
                "Queue_Id",
                "Kernel_Name",
                "Correlation_Id",
                "Start_Timestamp",
                "End_Timestamp",
            ]
        )
        writer.writerows(rows)
    return events


@given(dispatches=_DISPATCHES)
def test_kernel_and_family_totals_are_the_sums_of_the_dispatches(
    tmp_path_factory: pytest.TempPathFactory, dispatches: list[tuple[str, int, int]]
) -> None:
    root = tmp_path_factory.mktemp("trace")
    events = _write_kernel_trace(root, dispatches)
    expected: dict[str, float] = {}
    for name, _start, duration in events:
        expected[name] = expected.get(name, 0.0) + duration
    totals = rocprof_trace.kernel_time_totals(str(root))
    assert totals == pytest.approx(expected)
    families = rocprof_trace.family_time_totals(str(root))
    assert sum(families.values()) == pytest.approx(sum(expected.values()))


@given(dispatches=_DISPATCHES, bounds=st.tuples(st.integers(0, 200), st.integers(0, 200)))
def test_an_explicit_window_keeps_exactly_the_dispatches_that_start_inside_it(
    tmp_path_factory: pytest.TempPathFactory,
    dispatches: list[tuple[str, int, int]],
    bounds: tuple[int, int],
) -> None:
    root = tmp_path_factory.mktemp("trace")
    events = _write_kernel_trace(root, dispatches)
    start_us, end_us = sorted(bounds)
    base = min(start for _name, start, _duration in events)
    low, high = base + start_us * 1_000, base + end_us * 1_000
    expected: dict[str, float] = {}
    for name, start, duration in events:
        if low <= start <= high:
            expected[name] = expected.get(name, 0.0) + duration
    window = f"{start_us / 1e6}:{end_us / 1e6}"
    assert rocprof_trace.kernel_time_totals(str(root), window=window) == pytest.approx(expected)


_TOTALS = st.dictionaries(_KERNEL_NAMES, st.floats(0, 1e9, allow_nan=False), max_size=4)


@given(a=_TOTALS, b=_TOTALS)
def test_compare_deltas_are_antisymmetric(a: dict[str, float], b: dict[str, float]) -> None:
    forward = {name: (va, vb, delta) for name, va, vb, delta in rocprof_compare.delta_rows(a, b)}
    backward = {name: (va, vb, delta) for name, va, vb, delta in rocprof_compare.delta_rows(b, a)}
    assert forward.keys() == backward.keys() == set(a) | set(b)
    for name, (va, vb, delta) in forward.items():
        assert backward[name] == (vb, va, -delta)


def test_a_capture_compared_with_itself_has_no_deltas(capsys: pytest.CaptureFixture[str]) -> None:
    report = FIXTURES / "timeline_a"
    status, out, err = _run(capsys, "compare", "timeline", report, report)
    assert status == 0, err
    deltas = [line.rsplit("delta=", 1)[1] for line in out.splitlines() if "delta=" in line]
    assert deltas
    assert {delta.strip() for delta in deltas} == {"+0"}
