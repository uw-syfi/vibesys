"""Tests for the standalone torch.profiler trace analyzer.

``resources/profilers/torch/analyze_torch_profile.py`` is a standalone script
(no ``vibesys`` imports — it is copied into the agent's workspace as
``torch_profiler/``), so it is loaded by file path here rather than imported
as a package, mirroring ``tests/vibesys/loops/test_profiler_mcp.py``.

These tests exercise the analyzer's own functions directly (certify,
gemm-shapes, roofline, and the Chrome-trace -> summarized-report conversion
that lets the pre-existing ``kernels``/``operators``/etc. subcommands accept
a raw trace too), against small synthetic Kineto-shaped traces. Real
vLLM-captured MI210 traces were not available in this environment; these
fixtures follow the documented ``torch.profiler`` Chrome-trace export shape
(``traceEvents`` with ``cpu_op``/``kernel``/``hip_runtime``/``user_annotation``
categories, linked by "External id" and "correlation").
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import importlib.util
import io
import itertools
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parents[3]


def _load_analyzer():  # noqa: ANN202  # tracked: #288
    path = _REPO / "resources" / "profilers" / "torch" / "analyze_torch_profile.py"
    spec = importlib.util.spec_from_file_location("_analyze_torch_profile", str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    # Register in sys.modules before exec: the module's @dataclass decorators
    # (KW_ONLY resolution) look themselves up there via ``cls.__module__``.
    sys.modules[spec.name] = module
    parent = str(path.parent)
    inserted = parent not in sys.path
    if inserted:
        sys.path.insert(0, parent)
    try:
        assert spec.loader is not None
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


@pytest.fixture(scope="module")
def analyzer():  # noqa: ANN201  # tracked: #288
    return _load_analyzer()


def _run_capturing_stdout(
    fn: Callable[[argparse.Namespace], None], args: argparse.Namespace
) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(args)
    return buf.getvalue()


_next_external_id = itertools.count(1)


def _op(
    *,
    ts: int,
    dur: int,
    name: str = "aten::addmm",
    dims: tuple[list, list] | None = None,
    tid: int = 1,
) -> dict:
    """A ``cpu_op`` event with a fresh, unique "External id".

    Callers that need to link a kernel/launch to this op read the id back
    from the returned dict (``op["args"]["External id"]``) rather than
    choosing one up front, keeping this builder's own parameter count small.
    """
    args: dict = {"External id": next(_next_external_id)}
    if dims is not None:
        args["Input Dims"], args["Input type"] = dims
    return {
        "ph": "X",
        "cat": "cpu_op",
        "name": name,
        "pid": 1,
        "tid": tid,
        "ts": ts,
        "dur": dur,
        "args": args,
    }


def _launch(*, ts: int, external_id: int, correlation: int) -> dict:
    return {
        "ph": "X",
        "cat": "hip_runtime",
        "name": "hipLaunchKernel",
        "pid": 1,
        "tid": 1,
        "ts": ts,
        "dur": 2,
        "args": {"External id": external_id, "correlation": correlation},
    }


def _kernel(*, ts: int, dur: int, correlation: int, name: str = "Cijk_Ailk_Bljk_HHS_BH") -> dict:
    return {
        "ph": "X",
        "cat": "kernel",
        "name": name,
        "pid": 0,
        "tid": 2,
        "ts": ts,
        "dur": dur,
        "args": {"correlation": correlation, "device": 0},
    }


_ADDMM_DIMS = [[11008], [32, 4096], [4096, 11008]]
_ADDMM_TYPES = ["c10::BFloat16", "c10::BFloat16", "c10::BFloat16"]


def _gemm_call(i: int, *, ts: int, cpu_dur: int = 200, gpu_dur: int = 300) -> list[dict]:
    """One correlated aten::addmm -> hipLaunchKernel -> kernel triple."""
    op = _op(ts=ts, dur=cpu_dur, dims=(_ADDMM_DIMS, _ADDMM_TYPES))
    external_id = op["args"]["External id"]
    correlation = 5000 + i
    return [
        op,
        _launch(ts=ts + 5, external_id=external_id, correlation=correlation),
        _kernel(ts=ts + 10, dur=gpu_dur, correlation=correlation),
    ]


def _trace(events: list[dict], *, device_name: str = "AMD Instinct MI210") -> dict:
    return {
        "schemaVersion": 1,
        "deviceProperties": [{"id": 0, "name": device_name}],
        "traceEvents": events,
    }


class TestChromeTraceIndexing:
    def test_indexes_events_by_category(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = [
            {"ph": "M", "name": "process_name", "pid": 1, "args": {}},
            *_gemm_call(0, ts=100),
        ]
        index = analyzer._index_trace(_trace(events))

        assert len(index.cpu_ops) == 1
        assert len(index.runtime_launches) == 1
        assert len(index.kernels) == 1
        # cpu_op(100,+200), launch(105,+2), kernel(110,+300) -> [100, 410).
        assert index.trace_ts_min == 100
        assert index.trace_ts_max == 410

    def test_detects_device_from_properties(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        assert (
            analyzer._detect_device_key(_trace([], device_name="AMD Instinct MI300X")) == "mi300x"
        )
        assert (
            analyzer._detect_device_key(_trace([], device_name="NVIDIA H100 80GB HBM3")) == "h100"
        )
        assert analyzer._detect_device_key(_trace([], device_name="Unknown Accelerator")) is None


class TestCorrelation:
    def test_links_cpu_op_to_kernel_via_external_id_and_correlation(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = _gemm_call(0, ts=100, gpu_dur=333)
        index = analyzer._index_trace(_trace(events))

        op_to_kernels = analyzer._build_op_to_kernels(index)

        assert 0 in op_to_kernels
        assert len(op_to_kernels[0]) == 1
        assert op_to_kernels[0][0]["dur"] == 333

    def test_falls_back_to_external_id_stamped_directly_on_kernel(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        # Some exporters skip the runtime-launch hop and stamp "External id"
        # directly on the kernel event.
        op = _op(ts=100, dur=200, dims=(_ADDMM_DIMS, _ADDMM_TYPES))
        kernel = {
            "ph": "X",
            "cat": "kernel",
            "name": "some_kernel",
            "pid": 0,
            "tid": 2,
            "ts": 110,
            "dur": 90,
            "args": {"External id": op["args"]["External id"]},
        }
        index = analyzer._index_trace(_trace([op, kernel]))

        op_to_kernels = analyzer._build_op_to_kernels(index)

        assert op_to_kernels == {0: [kernel]}

    def test_no_correlation_when_external_ids_are_absent(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        op = {
            "ph": "X",
            "cat": "cpu_op",
            "name": "aten::addmm",
            "pid": 1,
            "tid": 1,
            "ts": 0,
            "dur": 1,
            "args": {},
        }
        kernel = {
            "ph": "X",
            "cat": "kernel",
            "name": "k",
            "pid": 0,
            "tid": 2,
            "ts": 1,
            "dur": 1,
            "args": {},
        }
        index = analyzer._index_trace(_trace([op, kernel]))

        assert analyzer._build_op_to_kernels(index) == {}


class TestSelfTime:
    def test_flat_calls_have_full_self_time(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        ops = [
            _op(ts=0, dur=100, name="aten::addmm"),
            _op(ts=200, dur=50, name="aten::addmm"),
        ]
        agg = analyzer._self_time_by_name(ops)

        assert agg["aten::addmm"]["self_us"] == 150.0
        assert agg["aten::addmm"]["total_us"] == 150.0
        assert agg["aten::addmm"]["count"] == 2

    def test_nested_call_subtracts_child_from_parent_self_time(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        # aten::linear (0..400) wraps a nested aten::addmm (10..390, dur 380).
        ops = [
            _op(ts=0, dur=400, name="aten::linear"),
            _op(ts=10, dur=380, name="aten::addmm"),
        ]
        agg = analyzer._self_time_by_name(ops)

        assert agg["aten::linear"]["self_us"] == 20.0
        assert agg["aten::linear"]["total_us"] == 400.0
        assert agg["aten::addmm"]["self_us"] == 380.0

    def test_self_time_is_grouped_per_thread(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        # Two threads with overlapping timestamps must not be treated as nested.
        ops = [
            _op(ts=0, dur=100, name="aten::mm", tid=1),
            _op(ts=10, dur=50, name="aten::mm", tid=2),
        ]
        agg = analyzer._self_time_by_name(ops)

        assert agg["aten::mm"]["self_us"] == 150.0


class TestMergedDuration:
    def test_merges_overlapping_intervals(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = [
            {"ts": 0, "dur": 100},
            {"ts": 50, "dur": 100},  # overlaps [0,100) -> extends to 150
            {"ts": 500, "dur": 10},  # disjoint
        ]
        assert analyzer._merged_duration(events) == 160.0

    def test_empty_is_zero(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        assert analyzer._merged_duration([]) == 0.0


class TestGemmShapeExtraction:
    @pytest.mark.parametrize(
        ("op_name", "dims", "expected"),
        [
            ("aten::mm", [[32, 4096], [4096, 11008]], (32, 11008, 4096, 1)),
            ("aten::matmul", [[32, 4096], [4096, 11008]], (32, 11008, 4096, 1)),
            ("aten::addmm", [[11008], [32, 4096], [4096, 11008]], (32, 11008, 4096, 1)),
            ("aten::linear", [[8, 16, 4096], [11008, 4096]], (128, 11008, 4096, 1)),
            ("aten::bmm", [[4, 32, 4096], [4, 4096, 11008]], (32, 11008, 4096, 4)),
            (
                "aten::baddbmm",
                [[4, 32, 11008], [4, 32, 4096], [4, 4096, 11008]],
                (32, 11008, 4096, 4),
            ),
        ],
    )
    def test_recognized_gemm_family_shapes(self, analyzer, op_name, dims, expected):  # noqa: ANN001, ANN201  # tracked: #288
        assert analyzer._shape_for_gemm_op(op_name, dims) == expected

    def test_rejects_mismatched_inner_dimension(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        assert analyzer._shape_for_gemm_op("aten::mm", [[32, 4096], [2048, 11008]]) is None

    def test_unrecognized_op_returns_none(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        assert analyzer._shape_for_gemm_op("aten::relu", [[32, 4096]]) is None

    def test_dedups_repeated_calls_and_sums_gpu_time(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events: list[dict] = []
        for i, gpu_dur in enumerate((300, 310, 350)):
            events.extend(_gemm_call(i, ts=100 + i * 500, gpu_dur=gpu_dur))
        index = analyzer._index_trace(_trace(events))
        op_to_kernels = analyzer._build_op_to_kernels(index)

        shapes = analyzer._extract_gemm_shapes(index, op_to_kernels)

        assert len(shapes) == 1
        shape = shapes[0]
        assert (shape.op, shape.m, shape.n, shape.k, shape.batch) == ("addmm", 32, 11008, 4096, 1)
        assert shape.call_count == 3
        assert shape.total_gpu_time_us == 960.0

    def test_skips_ops_missing_input_dims(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        op = _op(ts=0, dur=10)  # no Input Dims (record_shapes off)
        index = analyzer._index_trace(_trace([op]))
        shapes = analyzer._extract_gemm_shapes(index, {})
        assert shapes == []


class TestCertify:
    def test_pass_on_a_well_formed_trace(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = [
            {
                "ph": "X",
                "cat": "user_annotation",
                "name": "ProfilerStep#1",
                "pid": 1,
                "tid": 1,
                "ts": 0,
                "dur": 1000,
                "args": {},
            },
        ]
        for i in range(6):
            events.extend(_gemm_call(i, ts=10 + i * 100, cpu_dur=90, gpu_dur=90))
        index = analyzer._index_trace(_trace(events))
        op_to_kernels = analyzer._build_op_to_kernels(index)

        items = analyzer._certify(index, op_to_kernels)

        assert analyzer._certify_verdict(items) == "PASS"
        by_check = {item.check: item for item in items}
        assert by_check["gpu_kernels"].status == "PASS"
        assert by_check["record_shapes"].status == "PASS"
        assert by_check["step_markers"].status == "PASS"
        assert by_check["graph_replay"].status == "PASS"

    def test_fails_with_zero_kernels(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        op = _op(ts=0, dur=10, dims=(_ADDMM_DIMS, _ADDMM_TYPES))
        index = analyzer._index_trace(_trace([op]))

        items = analyzer._certify(index, {})

        assert analyzer._certify_verdict(items) == "FAIL"
        by_check = {item.check: item for item in items}
        assert by_check["gpu_kernels"].status == "FAIL"
        assert "ProfilerActivity" in by_check["gpu_kernels"].fix

    def test_fails_without_record_shapes(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = []
        for i in range(6):
            op = _op(ts=10 + i * 100, dur=90)  # no Input Dims
            external_id = op["args"]["External id"]
            events.extend(
                [
                    op,
                    _launch(ts=15 + i * 100, external_id=external_id, correlation=5000 + i),
                    _kernel(ts=20 + i * 100, dur=90, correlation=5000 + i),
                ]
            )
        index = analyzer._index_trace(_trace(events))
        op_to_kernels = analyzer._build_op_to_kernels(index)

        items = analyzer._certify(index, op_to_kernels)
        by_check = {item.check: item for item in items}

        assert by_check["record_shapes"].status == "FAIL"
        assert "record_shapes=True" in by_check["record_shapes"].fix

    def test_warns_on_graph_replay_launches(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = _gemm_call(0, ts=100)
        events.append(_kernel(ts=1000, dur=50, correlation=9999, name="hipGraphLaunch"))
        index = analyzer._index_trace(_trace(events))
        op_to_kernels = analyzer._build_op_to_kernels(index)

        items = analyzer._certify(index, op_to_kernels)
        by_check = {item.check: item for item in items}

        assert by_check["graph_replay"].status == "WARN"
        assert "graph replay" in by_check["graph_replay"].fix

    def test_fails_gpu_busy_when_window_is_mostly_idle(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = _gemm_call(0, ts=0, gpu_dur=10)
        # A kernel far in the future stretches the window without adding busy time.
        events.append(_kernel(ts=1_000_000, dur=1, correlation=8888))
        index = analyzer._index_trace(_trace(events))
        op_to_kernels = analyzer._build_op_to_kernels(index)

        items = analyzer._certify(index, op_to_kernels)
        by_check = {item.check: item for item in items}

        assert by_check["gpu_busy"].status == "FAIL"
        assert "sustained load" in by_check["gpu_busy"].fix


class TestRoofline:
    def test_gemm_flops_bytes(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        flops, total_bytes = analyzer._gemm_flops_bytes(32, 11008, 4096, 1, "c10::BFloat16")
        assert flops == 2.0 * 32 * 11008 * 4096
        assert total_bytes == (32 * 4096 + 4096 * 11008 + 32 * 11008) * 2

    def test_classifies_memory_bound_below_ridge(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        # MI210 ridge = 181e12 / 1600e9 ~= 113 FLOP/Byte. A skinny M=32 GEMM
        # in bf16 has AI well below that.
        metrics = analyzer._OpFlopsBytes(
            op_label="addmm",
            shape="32x11008x4096",
            dtype="c10::BFloat16",
            gpu_time_us=300.0,
            flops=2.0 * 32 * 11008 * 4096,
            total_bytes=(32 * 4096 + 4096 * 11008 + 32 * 11008) * 2,
        )
        row = analyzer._roofline_row(metrics, peak_tflops=181.0, peak_gbps=1600.0)
        assert row.bound == "memory"
        assert row.arithmetic_intensity < 113.0

    def test_classifies_compute_bound_above_ridge(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        # A large square GEMM has high arithmetic intensity -> compute-bound.
        m = n = k = 4096
        metrics = analyzer._OpFlopsBytes(
            op_label="mm",
            shape=f"{m}x{n}x{k}",
            dtype="c10::BFloat16",
            gpu_time_us=1000.0,
            flops=2.0 * m * n * k,
            total_bytes=(m * k + k * n + m * n) * 2,
        )
        row = analyzer._roofline_row(metrics, peak_tflops=181.0, peak_gbps=1600.0)
        assert row.bound == "compute"

    def test_resolve_peaks_prefers_explicit_over_device(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        args = argparse.Namespace(peak_tflops=100.0, peak_gbps=1000.0, device="mi210")
        tflops, gbps, label = analyzer._resolve_peaks(args, _trace([]))
        assert (tflops, gbps) == (100.0, 1000.0)
        assert "MI210" in label

    def test_resolve_peaks_autodetects_from_device_properties(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        args = argparse.Namespace(peak_tflops=None, peak_gbps=None, device=None)
        tflops, gbps, label = analyzer._resolve_peaks(
            args, _trace([], device_name="AMD Instinct MI300X")
        )
        assert tflops == analyzer._DEVICE_PEAKS["mi300x"].peak_tflops
        assert gbps == analyzer._DEVICE_PEAKS["mi300x"].peak_gbps
        assert "MI300X" in label

    def test_resolve_peaks_raises_when_nothing_available(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        args = argparse.Namespace(peak_tflops=None, peak_gbps=None, device=None)
        with pytest.raises(SystemExit, match="roofline needs --device"):
            analyzer._resolve_peaks(args, _trace([], device_name="Totally Unknown GPU"))

    def test_resolve_peaks_rejects_unknown_device_key(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        args = argparse.Namespace(peak_tflops=None, peak_gbps=None, device="mi9999")
        with pytest.raises(SystemExit, match="unknown --device"):
            analyzer._resolve_peaks(args, _trace([]))


class TestChromeTraceSummaryConversion:
    def test_summarize_chrome_trace_matches_summarize_prof_schema(self, analyzer):  # noqa: ANN001, ANN201  # tracked: #288
        events = _gemm_call(0, ts=0, cpu_dur=200, gpu_dur=300)
        summary = analyzer._summarize_chrome_trace(_trace(events))

        assert summary["total_cuda_time_us"] == 300.0
        assert summary["total_cpu_time_us"] == 200.0
        assert summary["device"] == "mi210"
        names = {e["name"] for e in summary["events"]}
        assert "aten::addmm" in names
        assert "Cijk_Ailk_Bljk_HHS_BH" in names

    def test_load_auto_detects_a_raw_trace_and_a_summarized_report(self, analyzer, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        trace_path = tmp_path / "trace.pt.trace.json"
        trace_path.write_text(json.dumps(_trace(_gemm_call(0, ts=0))))
        loaded = analyzer._load(str(trace_path))
        assert loaded["total_cuda_time_us"] == 300.0

        report_path = tmp_path / "prof.json"
        report_path.write_text(
            json.dumps(
                {"version": 1, "total_cuda_time_us": 1.0, "total_cpu_time_us": 1.0, "events": []}
            )
        )
        assert analyzer._load(str(report_path))["total_cuda_time_us"] == 1.0

    def test_load_reads_gzipped_trace(self, analyzer, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        trace_path = tmp_path / "trace.pt.trace.json.gz"
        with gzip.open(trace_path, "wt", encoding="utf-8") as f:
            json.dump(_trace(_gemm_call(0, ts=0)), f)

        loaded = analyzer._load(str(trace_path))
        assert loaded["total_cuda_time_us"] == 300.0


class TestCliCommands:
    def test_cmd_certify_rejects_a_summarized_report(self, analyzer, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        report_path = tmp_path / "prof.json"
        report_path.write_text(
            json.dumps(
                {"version": 1, "total_cuda_time_us": 1.0, "total_cpu_time_us": 1.0, "events": []}
            )
        )
        args = argparse.Namespace(trace=str(report_path))
        with pytest.raises(SystemExit, match="not a raw Kineto/Chrome trace"):
            analyzer.cmd_certify(args)

    def test_cmd_gemm_shapes_writes_json_and_prints_table(self, analyzer, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        trace_path = tmp_path / "trace.pt.trace.json"
        events: list[dict] = []
        for i, gpu_dur in enumerate((300, 310)):
            events.extend(_gemm_call(i, ts=100 + i * 500, gpu_dur=gpu_dur))
        trace_path.write_text(json.dumps(_trace(events)))
        out_path = tmp_path / "shapes.json"

        args = argparse.Namespace(trace=str(trace_path), top=20, out=str(out_path))
        out = _run_capturing_stdout(analyzer.cmd_gemm_shapes, args)

        assert "addmm" in out
        assert "32x11008x4096" in out
        written = json.loads(out_path.read_text())
        assert written == [
            {
                "op": "addmm",
                "m": 32,
                "n": 11008,
                "k": 4096,
                "batch": 1,
                "dtype": "c10::BFloat16",
                "call_count": 2,
                "total_gpu_time_us": 610.0,
            }
        ]

    def test_cmd_summary_runs_certify_before_the_usual_sections(self, analyzer, tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
        trace_path = tmp_path / "trace.pt.trace.json"
        trace_path.write_text(json.dumps(_trace(_gemm_call(0, ts=0))))

        args = argparse.Namespace(report=str(trace_path))
        out = _run_capturing_stdout(analyzer.cmd_summary, args)

        assert "Trace Certification" in out
        assert out.index("Trace Certification") < out.index("Top GPU Kernels")
