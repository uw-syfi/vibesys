"""Tests for the rocprofv3 trace analysis toolkit (AMD analog of analyze_nsys).

The ``resources.profilers.rocprof.analyze_rocprof`` import below resolves at
runtime because pytest's ``pythonpath = ["."]`` setting (pyproject.toml) puts
the repo root on ``sys.path``, the same way ``test_profiler.py`` imports
``resources.profilers.nsys.analyze_nsys``.

Fixtures are small synthetic rocprofv3 CSV files written per test, following
the documented rocprofv3 column names (``Kernel_Name``, ``Start_Timestamp``,
``Correlation_Id``, ...), laid out under a ``<hostname>/<pid>/`` subdirectory
the way rocprofv3 itself writes output.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import sqlite3
import string
from pathlib import Path

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st
from resources.profilers.rocprof import analyze_rocprof
from resources.profilers.rocprof.analyze_rocprof import (
    _CORR_COLS,
    _END_COLS,
    _KERNEL_NAME_COLS,
    _START_COLS,
    DiscoveredReport,
    _classify_family,
    _gaps_for_key,
    _get,
    _looks_like_triton_kernel,
    _normalize_direction,
    _outlier_family_note,
    _short_name,
    _union_duration,
    cmd_cpu_overhead,
    cmd_families,
    cmd_files,
    cmd_graphs,
    cmd_host_idle,
    cmd_idle_gaps,
    cmd_kernels,
    cmd_memory,
    cmd_query,
    cmd_summary,
    discover,
)
from tests.vibesys.loops.rocprof_strategies import (
    FAST,
    FEWER,
    case_variant,
    column_order,
    cpp_template_kernel_name,
    huge_kernel_name,
    intervals,
    permuted_csv,
    triton_jit_style_kernel_name,
    wrap_with_namespace_and_template_noise,
)

_REAL_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "rocprof" / "rocprofv3_real"

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _write(path: Path, header: str, *rows: str) -> None:
    path.write_text(header + "\n" + "\n".join(rows) + "\n")


def _process_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gpu-node-01" / "4242"
    d.mkdir(parents=True)
    return d


def _kernel_trace(d: Path, *rows: str) -> None:
    _write(
        d / "out_kernel_trace.csv",
        "Kernel_Name,Agent_Id,Queue_Id,Correlation_Id,Start_Timestamp,End_Timestamp,Pid",
        *rows,
    )


def _hip_api_trace(d: Path, *rows: str) -> None:
    _write(
        d / "out_hip_api_trace.csv",
        "Name,Correlation_Id,Start_Timestamp,End_Timestamp,Pid",
        *rows,
    )


def _memory_copy_trace(d: Path, *rows: str) -> None:
    _write(
        d / "out_memory_copy_trace.csv",
        "Direction,Start_Timestamp,End_Timestamp,Bytes",
        *rows,
    )


def _agent_info(d: Path, *rows: str) -> None:
    _write(
        d / "out_agent_info.csv",
        "Agent_Id,Product_Name,Gfx_Target_Version,Type",
        *rows,
    )


def _kernel_stats(d: Path, *rows: str) -> None:
    _write(
        d / "out_kernel_stats.csv",
        "Name,Calls,TotalDurationNs,MinNs,MaxNs",
        *rows,
    )


def _hip_api_stats(d: Path, *rows: str) -> None:
    _write(
        d / "out_hip_api_stats.csv",
        "Name,Calls,TotalDurationNs,MinNs,MaxNs",
        *rows,
    )


def _ns(report: str, **kwargs) -> argparse.Namespace:  # noqa: ANN003
    return argparse.Namespace(report=report, **kwargs)


# ---------------------------------------------------------------------------
# discover  # noqa: ERA001
# ---------------------------------------------------------------------------


def test_discover_finds_per_pid_layout(tmp_path: Path) -> None:
    d = _process_dir(tmp_path)
    _kernel_trace(d, "my_kernel,0,0,1,1000,2000,4242")
    _hip_api_trace(d, "hipLaunchKernel,1,900,1000,4242")

    disc = discover(str(tmp_path))

    assert disc.root == tmp_path
    assert len(disc.kernel_trace) == 1
    assert len(disc.hip_api_trace) == 1
    assert disc.kernel_trace[0].parent == d


def test_discover_accepts_a_single_file_directly(tmp_path: Path) -> None:
    d = _process_dir(tmp_path)
    _kernel_trace(d, "my_kernel,0,0,1,1000,2000,4242")

    disc = discover(str(d / "out_kernel_trace.csv"))

    assert disc.root == d
    assert len(disc.kernel_trace) == 1


def test_discover_rejects_a_missing_path(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="report path not found"):
        discover(str(tmp_path / "does-not-exist"))


def test_discover_buckets_unrecognized_and_json_and_db(tmp_path: Path) -> None:
    d = _process_dir(tmp_path)
    (d / "notes.csv").write_text("a,b\n1,2\n")
    (d / "out.json").write_text("{}")
    (d / "results.db").write_text("")

    disc = discover(str(tmp_path))

    assert len(disc.other_csv) == 1
    assert len(disc.json_files) == 1
    assert len(disc.db_files) == 1


# ---------------------------------------------------------------------------
# Small pure helpers
# ---------------------------------------------------------------------------


def test_short_name_shortens_deep_namespaces_but_keeps_last_two_segments() -> None:
    assert _short_name("ck::tensor_operation::device::DeviceGemm_Xdl") == "device::DeviceGemm_Xdl"
    assert _short_name("simple_kernel") == "simple_kernel"
    assert _short_name("a::b::c::func") == "c::func"


def test_normalize_direction_recognizes_common_spellings() -> None:
    assert _normalize_direction("HostToDevice") == "HtoD"
    assert _normalize_direction("DEVICE_TO_HOST") == "DtoH"
    assert _normalize_direction("d2d") == "DtoD"
    assert _normalize_direction("weird") == "weird"


def test_classify_family_matches_expected_libraries() -> None:
    assert (
        _classify_family("ck::tensor_operation::device::DeviceGemm_Xdl")
        == "Composable Kernel (ck::/ck_tile)"
    )
    assert _classify_family("Cijk_Alik_Bljk_HHS_BH_MT128x128x16") == "hipBLASLt / Tensile (Cijk_*)"
    assert _classify_family("rocblas_gemm_ex") == "rocBLAS"
    assert _classify_family("MIOpenConvUni") == "MIOpen"
    assert _classify_family("ncclDevKernel_AllReduce_Sum_f32_RING_LL") == "RCCL"
    assert _classify_family("paged_attention_v1_kernel") == "vLLM/SGLang custom ops"
    assert _classify_family("triton_poi_fused_add_0") == "Triton (JIT)"
    assert (
        _classify_family("void at::native::vectorized_elementwise_kernel<4>")
        == "PyTorch native (at::native)"
    )
    assert _classify_family("something_completely_unrecognized") == "other"


def test_classify_family_survives_a_deep_aiter_namespace() -> None:
    """Classification must run before name-shortening trims namespace markers.

    ``_short_name`` keeps only the last two ``::``-separated segments for
    display; a naive pipeline that classified the *shortened* name would lose
    an "aiter" marker buried more than two segments deep. Regression guard
    for that ordering bug.
    """
    deep_name = "aiter::device::gemm::asm::SomeAsmKernel"
    assert "aiter" not in _short_name(deep_name)  # shortening does lose the marker...
    assert _classify_family(deep_name) == "AITER (asm/ck)"  # ...but classification still sees it.


# ---------------------------------------------------------------------------
# cmd_files
# ---------------------------------------------------------------------------


def test_cmd_files_reports_counts_processes_and_agents(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_trace(d, "my_kernel,0,0,1,1000,2000,4242")
    _hip_api_trace(d, "hipLaunchKernel,1,900,1000,4242")
    _agent_info(d, "0,AMD Instinct MI210,90a10,GPU")

    cmd_files(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "kernel_trace" in out
    assert "1 file(s), 1 row(s)" in out
    assert "pid=4242" in out
    assert "host=gpu-node-01" in out
    assert "AMD Instinct MI210" in out
    assert "gfx=90a10" in out


def test_cmd_files_reports_nothing_found_for_an_empty_dir(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    cmd_files(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "no rocprofv3 output recognized" in out


# ---------------------------------------------------------------------------
# cmd_kernels / cmd_families
# ---------------------------------------------------------------------------


def _mixed_family_trace(d: Path) -> None:
    _kernel_trace(
        d,
        "ck::tensor_operation::device::DeviceGemm_Xdl,0,0,101,1000,5000,4242",
        "Cijk_Alik_Bljk_HHS_BH_MT128x128x16,0,0,102,10000,14000,4242",
        "triton_poi_fused_add_0,0,0,103,20000,21000,4242",
        "flash_fwd_attn_kernel,0,0,104,100000,180000,4242",
    )


def test_cmd_kernels_ranks_by_total_time_and_labels_family(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _mixed_family_trace(d)

    cmd_kernels(_ns(str(tmp_path), top=10))
    out = capsys.readouterr().out

    assert out.index("flash_fwd_attn_kernel") < out.index("device::DeviceGemm_Xdl")
    assert "Composable Kernel (ck::/ck_tile)" in out
    assert "hipBLASLt / Tensile (Cijk_*)" in out
    assert "Triton (JIT)" in out
    assert "Total GPU kernel time" in out


def test_cmd_kernels_reports_no_data_for_an_empty_report(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    cmd_kernels(_ns(str(tmp_path), top=10))
    out = capsys.readouterr().out
    assert "no kernel data found" in out


def test_cmd_kernels_falls_back_to_stats_when_trace_is_absent(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_stats(d, "rocblas_gemm_ex,10,50000,4000,6000")

    cmd_kernels(_ns(str(tmp_path), top=10))
    out = capsys.readouterr().out

    assert "Kernel data source: stats" in out
    assert "rocblas_gemm_ex" in out
    assert "rocBLAS" in out


def test_cmd_families_flags_gemm_or_attention_in_a_fallback_family(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _mixed_family_trace(d)

    cmd_families(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "Composable Kernel (ck::/ck_tile)" in out
    assert "*** Finding:" in out
    assert "flash_fwd_attn_kernel" in out
    assert "AITER_LOG_TUNED_CONFIG" in out


def test_cmd_families_does_not_flag_a_clean_aiter_dominant_trace(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_trace(d, "aiter::fmha_fwd_v3_kernel,0,0,101,1000,90000,4242")

    cmd_families(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "AITER (asm/ck)" in out
    assert "*** Finding:" not in out


def test_cmd_families_percent_gpu_uses_merged_busy_time_not_naive_sum_of_overlapping_queues(  # noqa: ANN201
    tmp_path,  # noqa: ANN001
    capsys,  # noqa: ANN001
):
    # Regression for the %GPU denominator bug: two families dispatched on
    # DIFFERENT HW queues of the same GPU but fully overlapping in
    # wall-clock time (real on rocprofv3 vLLM-serving captures with
    # concurrent queues -- see the rocprof worklog's %GPU denominator note,
    # and analyze_rocprof.py's module docstring on per-(agent,queue)
    # merged-interval accounting). Summing each kernel's own duration
    # double-counts the overlap: the old code reported each family at 50%
    # of an artificially 2x-inflated "total" (1000ns each / 2000ns naive
    # sum), silently hiding that the GPU spent its whole 1000ns window on
    # BOTH families at once. The merged-interval union recognizes the GPU
    # was only ever busy for a 1000ns (here: 10ms) window, so each family --
    # which occupied that entire window on its own queue -- correctly shows
    # ~100% of the union, and a note calls out the concurrent overlap.
    d = _process_dir(tmp_path)
    _kernel_trace(
        d,
        "ck::tensor_operation::device::DeviceGemm_Xdl,0,1,101,0,10000000,4242",
        "Cijk_Alik_Bljk_HHS_BH_MT128x128x16,0,2,102,0,10000000,4242",
    )

    cmd_families(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "Composable Kernel (ck::/ck_tile)" in out
    assert "hipBLASLt / Tensile (Cijk_*)" in out
    assert " 50.0%" not in out
    assert "100.0%" in out
    assert "Note:" in out
    assert "overlaps across HW queues" in out


@given(gap_ns=st.integers(min_value=0, max_value=10_000_000))
@FAST
def test_gpu_busy_denominator_never_exceeds_the_naive_sum(tmp_path_factory, gap_ns):  # noqa: ANN001, ANN201
    # Generalizes the regression above: for ANY separation between two
    # same-duration kernels on different queues (from fully overlapping,
    # gap_ns=0, to fully disjoint), the merged-union %GPU denominator must
    # never exceed the naive per-kernel-duration sum -- merging intervals can
    # only remove double-counted overlap, never add time that wasn't there.
    root = tmp_path_factory.mktemp("gpu-busy-denom")
    d = _process_dir(root)
    dur = 10_000_000
    _kernel_trace(
        d,
        f"ck::device::DeviceGemm_Xdl,0,1,101,0,{dur},4242",
        f"Cijk_Alik_Bljk_HHS_BH_MT128x128x16,0,2,102,{gap_ns},{gap_ns + dur},4242",
    )

    disc = discover(str(root))
    bundle = analyze_rocprof._get_kernel_bundle(disc)  # noqa: SLF001
    naive_sum = sum(e["total_ns"] for e in bundle.by_name.values())
    denom = analyze_rocprof._gpu_busy_denominator_ns(disc, naive_sum)  # noqa: SLF001
    assert denom <= naive_sum + 1e-6
    # And when the two windows don't overlap at all, merging changes nothing.
    if gap_ns >= dur:
        assert denom == pytest.approx(naive_sum)


# ---------------------------------------------------------------------------
# cmd_idle_gaps
# ---------------------------------------------------------------------------


def test_cmd_idle_gaps_finds_the_largest_gap(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_trace(
        d,
        "kernel_a,0,0,1,0,1000,4242",
        "kernel_b,0,0,2,1100,2000,4242",
        # a big gap here
        "kernel_c,0,0,3,100000,101000,4242",
    )

    cmd_idle_gaps(_ns(str(tmp_path), top=5))
    out = capsys.readouterr().out

    assert "Idle gaps found: 1" in out
    assert "kernel_b" in out
    assert "kernel_c" in out


def test_cmd_idle_gaps_needs_a_trace_not_just_stats(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_stats(d, "some_kernel,5,10000,1000,3000")

    cmd_idle_gaps(_ns(str(tmp_path), top=5))
    out = capsys.readouterr().out

    assert "needs per-event timestamps" in out


# ---------------------------------------------------------------------------
# cmd_cpu_overhead
# ---------------------------------------------------------------------------


def test_cmd_cpu_overhead_computes_matched_launch_bound_ratio(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    # CPU launch overhead (100ns) exceeds GPU exec time (50ns) -> launch-bound.
    _kernel_trace(d, "tiny_kernel,0,0,1,1000,1050,4242")
    _hip_api_trace(
        d,
        "hipLaunchKernel,1,800,900,4242",
        "hipStreamSynchronize,0,1050,1100,4242",
    )

    cmd_cpu_overhead(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "Synchronization stalls: 1 calls" in out
    assert "matched by Correlation_Id" in out
    assert "LAUNCH-BOUND" in out


def test_cmd_cpu_overhead_falls_back_to_coarse_stats_ratio(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_stats(d, "some_kernel,5,1000,100,300")
    _hip_api_stats(d, "hipLaunchKernel,5,5000,800,1200")

    cmd_cpu_overhead(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "aggregated from *_hip_api_stats.csv" in out
    assert "Coarse CPU-launch-time / GPU-kernel-time ratio" in out


def test_cmd_cpu_overhead_reports_no_data(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    cmd_cpu_overhead(_ns(str(tmp_path)))
    out = capsys.readouterr().out
    assert "no HIP API data found" in out


# ---------------------------------------------------------------------------
# cmd_memory
# ---------------------------------------------------------------------------


def test_cmd_memory_reports_direction_bytes_and_bandwidth(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _memory_copy_trace(
        d,
        "HostToDevice,0,1000,1000000",
        "DeviceToHost,2000,2500,500000",
    )

    cmd_memory(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "HtoD" in out
    assert "DtoH" in out
    assert "GB/s" in out
    assert "Total memory copy" in out


def test_cmd_memory_reports_no_data(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    cmd_memory(_ns(str(tmp_path)))
    out = capsys.readouterr().out
    assert "no memory copy data found" in out


# ---------------------------------------------------------------------------
# cmd_graphs
# ---------------------------------------------------------------------------


def test_cmd_graphs_reports_healthy_attribution_with_no_graph_launches(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_trace(d, "kernel_a,0,0,1,0,1000,4242")
    _hip_api_trace(d, "hipLaunchKernel,1,0,100,4242")

    cmd_graphs(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "hipGraphLaunch calls: 0" in out
    assert "should be reliable" in out


def test_cmd_graphs_flags_degraded_attribution_under_heavy_graph_use(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    # One hipGraphLaunch call fans out into many kernels sharing its
    # Correlation_Id, and there is only one direct hipLaunchKernel call.
    kernel_rows = [f"replayed_kernel_{i},0,0,999,{i * 100},{i * 100 + 50},4242" for i in range(20)]
    kernel_rows.append("standalone_kernel,0,0,1,5000,5050,4242")
    _kernel_trace(d, *kernel_rows)
    _hip_api_trace(
        d,
        "hipGraphLaunch,999,0,10,4242",
        "hipLaunchKernel,1,4900,5000,4242",
    )

    cmd_graphs(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "hipGraphLaunch calls: 1" in out
    assert "DEGRADED" in out
    assert "eager mode" in out
    assert "matched kernels (by Correlation_Id): 1" in out


# ---------------------------------------------------------------------------
# cmd_host_idle
# ---------------------------------------------------------------------------


def test_cmd_host_idle_flags_a_mostly_idle_capture(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    # One tiny kernel inside a huge capture window -> mostly host-idle.
    _kernel_trace(d, "one_kernel,0,0,1,0,10,4242")
    _hip_api_trace(d, "hipLaunchKernel,1,0,10000000,4242")

    cmd_host_idle(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "HOST-IDLE" in out
    assert "re-capture under active load" in out


def test_cmd_host_idle_verdict_ok_when_gpu_is_active(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_trace(
        d,
        "kernel_a,0,0,1,0,900000,4242",
        "kernel_b,0,0,2,900000,1000000,4242",
    )

    cmd_host_idle(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "trace looks valid" in out


def test_cmd_host_idle_reports_no_timestamped_data(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _kernel_stats(d, "some_kernel,5,1000,100,300")

    cmd_host_idle(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "no per-event timestamps" in out


# ---------------------------------------------------------------------------
# cmd_query
# ---------------------------------------------------------------------------


def test_cmd_query_explains_when_no_rocpd_db_is_present(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    cmd_query(_ns(str(tmp_path), sql="select 1"))
    out = capsys.readouterr().out
    assert "no rocpd SQLite" in out


def test_cmd_query_runs_sql_against_a_rocpd_db(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    db_path = d / "trace.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE t (x INTEGER, y TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'a'), (2, 'b')")
    conn.commit()
    conn.close()

    cmd_query(_ns(str(tmp_path), sql="SELECT * FROM t ORDER BY x"))
    out = capsys.readouterr().out

    assert "x\ty" in out
    assert "1\ta" in out
    assert "2\tb" in out


def test_cmd_query_reports_sql_errors_and_exits_nonzero(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    sqlite3.connect(str(d / "trace.db")).close()

    with pytest.raises(SystemExit) as exc_info:
        cmd_query(_ns(str(tmp_path), sql="not valid sql"))

    assert exc_info.value.code == 1
    assert "SQL error" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# cmd_summary
# ---------------------------------------------------------------------------


def test_cmd_summary_runs_every_section_and_stays_prompt_sized(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _mixed_family_trace(d)
    _hip_api_trace(
        d,
        "hipLaunchKernel,101,900,1000,4242",
        "hipLaunchKernel,102,9800,10000,4242",
        "hipLaunchKernel,103,19900,20000,4242",
        "hipLaunchKernel,104,99900,100000,4242",
    )
    _memory_copy_trace(d, "HostToDevice,0,500,4096")
    _agent_info(d, "0,AMD Instinct MI210,90a10,GPU")

    cmd_summary(_ns(str(tmp_path), top=10))
    out = capsys.readouterr().out

    for heading in (
        "## Files",
        "## Trace Validity (host-idle check)",
        "## Top Kernels",
        "## Kernel Library Families",
        "## GPU Idle Gaps",
        "## CPU / HIP API Overhead",
        "## Memory Copies",
        "## HIP Graph Launches",
    ):
        assert heading in out

    # Every section must respect the hard per-section line cap: no run of
    # more than 40 consecutive non-heading lines.
    lines = out.splitlines()
    longest_run = 0
    current_run = 0
    for line in lines:
        if line.startswith(("## ", "=")):
            current_run = 0
            continue
        current_run += 1
        longest_run = max(longest_run, current_run)
    assert longest_run <= 41  # 40 content lines + one "... more lines omitted" marker


# ---------------------------------------------------------------------------
# DiscoveredReport is a plain, empty-by-default container
# ---------------------------------------------------------------------------


def test_discovered_report_defaults_to_empty_lists(tmp_path: Path) -> None:
    disc = DiscoveredReport(root=tmp_path)
    assert disc.kernel_trace == []
    assert disc.db_files == []


# ---------------------------------------------------------------------------
# Property: family classification is invariant to display-shortening and
# namespace/template noise
# ---------------------------------------------------------------------------

_MARKER_FAMILY: dict[str, str] = {
    "aiter": "AITER (asm/ck)",
    "ck::": "Composable Kernel (ck::/ck_tile)",
    "ck_tile::": "Composable Kernel (ck::/ck_tile)",
    "cijk_": "hipBLASLt / Tensile (Cijk_*)",
    "rocblas_": "rocBLAS",
    "miopen": "MIOpen",
    "triton_": "Triton (JIT)",
    "at::native": "PyTorch native (at::native)",
    "rccl": "RCCL",
    "paged_attention": "vLLM/SGLang custom ops",
    "rotary_embedding": "vLLM/SGLang custom ops",
}


@given(marker=st.sampled_from(tuple(_MARKER_FAMILY)), data=st.data())
@FAST
def test_classify_family_is_invariant_to_short_name_and_namespace_noise(marker, data):  # noqa: ANN001, ANN201  # tracked: #288
    name = data.draw(wrap_with_namespace_and_template_noise(marker))
    expected = _MARKER_FAMILY[marker]
    # The random namespace-noise prefix can occasionally spell out a
    # *different* marker (e.g. a "ck" prefix segment in front of "cijk_"
    # produces "ck::cijk_", which legitimately classifies as Composable
    # Kernel since that rule is checked first) -- ordered first-match-wins
    # behavior, not an invariance violation. Skip that rare collision rather
    # than asserting an ordering `_classify_family` never promised.
    other_markers = [m for m in _MARKER_FAMILY if m != marker]
    assume(not any(m in name.lower() for m in other_markers))

    family_from_full = _classify_family(name)
    family_from_shortened = _classify_family(_short_name(name))

    assert family_from_full == expected
    assert family_from_shortened == family_from_full
    assert family_from_full != "other"


# ---------------------------------------------------------------------------
# Property: CSV column discovery/parsing survives column order, casing, and
# unrecognized columns; a header-only CSV is handled cleanly.
# ---------------------------------------------------------------------------

_KNOWN_COLUMN_SPECS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("kernel_name_val", _KERNEL_NAME_COLS),
    ("1234.5", _START_COLS),
    ("6789.5", _END_COLS),
    ("corr_42", _CORR_COLS),
)


@given(data=st.data())
@FAST
def test_get_resolves_known_aliases_under_permutation_case_and_unknown_columns(data):  # noqa: ANN001, ANN201  # tracked: #288
    names = []
    values = []
    for value, aliases in _KNOWN_COLUMN_SPECS:
        alias = data.draw(case_variant(data.draw(st.sampled_from(aliases))))
        names.append(alias)
        values.append(value)
    # An extra, unrecognized column mixed in must not interfere.
    names.append("Some_Unknown_Column")
    values.append("noise")

    order = data.draw(column_order(len(names)))
    csv_text = permuted_csv(names, [values], order)
    row = next(csv.DictReader(io.StringIO(csv_text)))

    for expected, aliases in _KNOWN_COLUMN_SPECS:
        assert _get(row, aliases) == expected


def test_cmd_kernels_handles_a_header_only_kernel_trace_cleanly(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    (d / "out_kernel_trace.csv").write_text("Kernel_Name,Start_Timestamp,End_Timestamp\n")

    cmd_kernels(_ns(str(tmp_path), top=10))
    out = capsys.readouterr().out

    assert "no kernel data found" in out


# ---------------------------------------------------------------------------
# Property: per-PID directory discovery works at any nesting depth
# ---------------------------------------------------------------------------

_DIR_NAME = st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=12)


@given(parts=st.lists(_DIR_NAME, min_size=0, max_size=3))
@FEWER
def test_discover_finds_trace_files_under_arbitrary_nesting_depth(tmp_path_factory, parts):  # noqa: ANN001, ANN201  # tracked: #288
    root = tmp_path_factory.mktemp("nesting")
    d = root.joinpath(*parts) if parts else root
    d.mkdir(parents=True, exist_ok=True)
    (d / "out_kernel_trace.csv").write_text("Kernel_Name,Start_Timestamp,End_Timestamp\nk,0,10\n")
    (d / "out_hip_api_trace.csv").write_text(
        "Name,Start_Timestamp,End_Timestamp\nhipLaunchKernel,0,1\n"
    )

    disc = discover(str(root))

    assert len(disc.kernel_trace) == 1
    assert disc.kernel_trace[0].parent == d
    assert len(disc.hip_api_trace) == 1
    assert disc.hip_api_trace[0].parent == d


# ---------------------------------------------------------------------------
# Property: time-window busy/idle accounting never double-counts and never
# goes negative
# ---------------------------------------------------------------------------


def test_union_duration_merges_overlapping_intervals_without_double_counting():  # noqa: ANN201  # tracked: #288
    events = [
        {"start_ns": 0.0, "end_ns": 100.0},
        {"start_ns": 50.0, "end_ns": 150.0},  # overlaps
        {"start_ns": 150.0, "end_ns": 200.0},  # touches
    ]
    assert _union_duration(events) == 200.0


@given(ivals=intervals())
@FAST
def test_union_duration_never_exceeds_the_window(ivals):  # noqa: ANN001, ANN201  # tracked: #288
    events = [{"start_ns": float(s), "end_ns": float(s + d)} for s, d in ivals]
    busy = _union_duration(events)

    assert busy >= 0.0
    if not events:
        assert busy == 0.0
        return
    window = max(e["end_ns"] for e in events) - min(e["start_ns"] for e in events)
    assert busy <= window


@given(ivals=intervals())
@FAST
def test_gaps_for_key_busy_plus_all_gaps_equals_window_with_no_negative_gap(ivals):  # noqa: ANN001, ANN201  # tracked: #288
    # Force every positive gap to be reported (no threshold filtering), so
    # busy + idle can be checked against the window exactly using only real
    # merge/gap code, not a reimplementation of it. Set directly on the module
    # (not via the function-scoped `monkeypatch` fixture, which hypothesis
    # flags as unsafe to reuse across `@given` examples) and restore it
    # unconditionally afterwards.
    original_threshold = analyze_rocprof._IDLE_GAP_THRESHOLD_NS  # noqa: SLF001  # tracked: #288
    analyze_rocprof._IDLE_GAP_THRESHOLD_NS = -1.0  # noqa: SLF001  # tracked: #288
    try:
        evs = [(float(s), float(s + d), f"k{i}") for i, (s, d) in enumerate(ivals)]
        busy_ns, gaps = _gaps_for_key(("agent0", "queue0"), evs)
    finally:
        analyze_rocprof._IDLE_GAP_THRESHOLD_NS = original_threshold  # noqa: SLF001  # tracked: #288

    assert all(gap[3] >= 0 for gap in gaps)
    if len(evs) < 2:
        return
    window = max(e for _s, e, _n in evs) - min(s for s, _e, _n in evs)
    idle_ns = sum(gap[3] for gap in gaps)
    assert busy_ns >= 0.0
    assert busy_ns <= window
    assert busy_ns + idle_ns == pytest.approx(window)


def _write_kernel_trace_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


@given(name=huge_kernel_name())
@FEWER
def test_cmd_kernels_and_idle_gaps_bound_line_width_for_huge_kernel_names(tmp_path_factory, name):  # noqa: ANN001, ANN201  # tracked: #288
    root = tmp_path_factory.mktemp("huge-name")
    d = _process_dir(root)
    _write_kernel_trace_csv(
        d / "out_kernel_trace.csv",
        [
            "Kernel_Name",
            "Agent_Id",
            "Queue_Id",
            "Correlation_Id",
            "Start_Timestamp",
            "End_Timestamp",
            "Pid",
        ],
        [
            [name, 0, 0, 1, 0, 1000, 4242],
            [name, 0, 0, 2, 1_000_000, 1_001_000, 4242],
        ],
    )

    # Capture stdout manually (not via the function-scoped `capsys` fixture,
    # which hypothesis flags as unsafe to reuse across `@given` examples).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_kernels(_ns(str(root), top=10))
        cmd_idle_gaps(_ns(str(root), top=10))
    out = buf.getvalue()

    assert out
    for line in out.splitlines():
        assert len(line) <= 200


# ---------------------------------------------------------------------------
# Regression: GDN / generic-Triton kernels were misclassified as "other"
#
# Validated against a real vLLM Qwen3.5-9B/MI210 rocprofv3 capture
# (`amd-profiler-samples/rocprofv3_trace_{eager,graph}`): rocprofv3 only
# renames a kernel with a `triton_*` prefix when it comes through
# torch.inductor. A kernel compiled directly from `@triton.jit` -- every
# GDN (gated-delta-net) kernel vLLM/SGLang dispatch for Qwen3.5, plus most of
# vLLM's own sampling/mamba Triton kernels -- keeps its Python function name
# verbatim and fell through every `_FAMILY_RULES` entry to "other" before
# this fix (confirmed by reverting `_classify_family`'s `_looks_like_triton_kernel`
# fallback: the real-trace `families` cross-check below then shows "other" at
# ~6% of GPU time with these kernels as its top entry instead of "Triton (JIT)").
# ---------------------------------------------------------------------------


def test_classify_family_recognizes_gdn_and_generic_triton_kernels_without_a_triton_prefix() -> (
    None
):
    assert (
        _classify_family("fused_recurrent_gated_delta_rule_packed_decode_kernel") == "Triton (JIT)"
    )
    assert _classify_family("chunk_gated_delta_rule_fwd_kernel_h_blockdim64") == "Triton (JIT)"
    assert _classify_family("_topk_topp_kernel") == "Triton (JIT)"
    assert _classify_family("rotary_kernel") == "Triton (JIT)"


def test_classify_family_does_not_misclassify_templated_or_mangled_names_as_triton() -> None:
    """A hand-written HIP/C++ kernel must not be swept into Triton by the
    underscore/``_kernel``-suffix heuristic: template args (``<...>``) and an
    un-demangled Itanium ``_Z...`` symbol both rule it out.
    """
    assert (
        _classify_family("void wvSplitK_hf_sml_<__hip_bfloat16, 64, 4, 16, 8, 2, 2>(int, int, int)")
        == "vLLM/SGLang custom ops"
    )
    assert not _looks_like_triton_kernel(
        "void some_other_op_<__hip_bfloat16, 64>(int, int, int, int)"
    )
    assert not _looks_like_triton_kernel("_ZN4vllm18act_and_mul_kernelIN3c108BFloat16EEEvv")
    assert not _looks_like_triton_kernel("__amd_rocclr_copyBuffer")


@given(name=triton_jit_style_kernel_name())
@FAST
def test_classify_family_matches_triton_jit_style_names(name):  # noqa: ANN001, ANN201  # tracked: #288
    assert _classify_family(name) == "Triton (JIT)"


@given(name=cpp_template_kernel_name())
@FAST
def test_classify_family_never_matches_templated_names_via_the_triton_fallback(name):  # noqa: ANN001, ANN201  # tracked: #288
    # The base name is deliberately built to look Triton-shaped (leading
    # underscore chance, "_kernel" substring); only the "<...>" template args
    # should rule out the fallback, regardless of what _FAMILY_RULES pattern
    # (if any) the random base name happens to also dodge.
    assert not _looks_like_triton_kernel(name)


# ---------------------------------------------------------------------------
# Regression: memory_copy_trace.csv with no Bytes/Size column used to print
# a misleading "Total memory copy: 0.00 GB" / "0.0GB/s" instead of saying so
#
# Confirmed on the real vLLM captures in amd-profiler-samples: rocprofv3's
# `*_memory_copy_trace.csv` there has no Bytes/Size column at all
# (`Kind,Direction,Stream_Id,Source_Agent_Id,Destination_Agent_Id,
# Correlation_Id,Start_Timestamp,End_Timestamp`), even though 18k real HtoD
# copies happened. The old code silently defaulted missing bytes to 0 and
# printed it as if it were a real (zero) measurement.
# ---------------------------------------------------------------------------


def test_cmd_memory_reports_bytes_unavailable_instead_of_a_misleading_zero(tmp_path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    d = _process_dir(tmp_path)
    _write(
        d / "out_memory_copy_trace.csv",
        "Direction,Start_Timestamp,End_Timestamp",
        "HostToDevice,0,1000",
        "HostToDevice,2000,2500",
    )

    cmd_memory(_ns(str(tmp_path)))
    out = capsys.readouterr().out

    assert "0.00 GB" not in out
    assert "GB/s" not in out
    assert "byte counts not available" in out
    assert "Total memory copy time" in out
    assert "HtoD" in out


@given(byte_counts=st.lists(st.integers(min_value=0, max_value=10_000_000), min_size=1, max_size=6))
@FAST
def test_cmd_memory_bytes_available_never_prints_unavailable_note(tmp_path_factory, byte_counts):  # noqa: ANN001, ANN201  # tracked: #288
    root = tmp_path_factory.mktemp("mem-bytes")
    d = _process_dir(root)
    rows = [f"HostToDevice,{i * 10000},{i * 10000 + 500},{b}" for i, b in enumerate(byte_counts)]
    _write(d / "out_memory_copy_trace.csv", "Direction,Start_Timestamp,End_Timestamp,Bytes", *rows)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_memory(_ns(str(root)))
    out = buf.getvalue()

    assert "byte counts not available" not in out
    assert "Bandwidth" in out


# ---------------------------------------------------------------------------
# Real-trace fixtures: trimmed excerpts of the real vLLM Qwen3.5-9B/MI210
# rocprofv3 captures (kernel names, correlation ids, and timestamps copied
# verbatim from amd-profiler-samples/rocprofv3_trace_{eager,graph}), laid out
# the way rocprofv3 itself writes a <hostname>/<pid>/ capture directory. See
# fixtures/rocprof/rocprofv3_real/ for the generation source.
# ---------------------------------------------------------------------------


def test_families_on_the_real_eager_trace_classifies_every_sampled_family_correctly() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_families(_ns(str(_REAL_FIXTURE_ROOT / "eager")))
    out = buf.getvalue()

    assert "Composable Kernel (ck::/ck_tile)" in out
    assert "hipBLASLt / Tensile (Cijk_*)" in out
    assert "Triton (JIT)" in out
    assert "vLLM/SGLang custom ops" in out
    assert "PyTorch native (at::native)" in out
    # The real bug: GDN/wvSplitK kernels must not land in a generic bucket.
    assert not out.splitlines()[-1].startswith("other")


def test_families_on_the_real_graph_trace_classifies_the_replayed_kernels_too() -> None:
    """The kernels replayed under the real ``hipGraphLaunch`` fan-out (a
    Triton pointwise op and three hipBLASLt/Tensile GEMM variants) must be
    classified the same as their direct-launch counterparts.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_families(_ns(str(_REAL_FIXTURE_ROOT / "graph")))
    out = buf.getvalue()

    assert "Composable Kernel (ck::/ck_tile)" in out
    assert "hipBLASLt / Tensile (Cijk_*)" in out
    assert "Triton (JIT)" in out
    assert "vLLM/SGLang custom ops" in out


def test_families_on_the_real_graph_trace_flags_the_implausible_fmha_outlier() -> None:
    """Regression for the "27 calls, 9.13s, 338ms/call, 31% GPU" investigation
    (docs/contributing/amd-profiler-worklog.md): the real graph-mode trace's
    Composable Kernel FmhaFwdKernel dispatches (trimmed to 3 of the real 27 in
    this fixture) average ~336ms/call while every other sampled family
    averages tens-to-hundreds of *microseconds* per call -- a >1000x outlier
    that must not pass through silently as a plain %GPU line.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_families(_ns(str(_REAL_FIXTURE_ROOT / "graph")))
    out = buf.getvalue()

    assert "*** Finding: 'Composable Kernel (ck::/ck_tile)' averages" in out
    assert "ms/call" in out
    assert "x the median family's per-call average" in out
    assert "compute.py profile" in out
    assert "att.py" in out


@given(
    avgs_ns=st.lists(
        st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
        min_size=2,
        max_size=8,
    )
)
@FAST
def test_outlier_family_note_never_fires_when_every_family_is_within_the_threshold(  # noqa: ANN201
    avgs_ns: list[float],
):
    # If every family's per-call average is within _OUTLIER_AVG_MULTIPLE of
    # the smallest one, nothing here looks like the FmhaFwdKernel anomaly --
    # the note must stay silent (no false positives on an ordinary, if
    # uneven, mix of kernel costs).
    smallest = min(avgs_ns)
    assume(all(avg <= smallest * 20 for avg in avgs_ns))
    ordered = [(f"family_{i}", {"calls": 1, "total_ns": avg}) for i, avg in enumerate(avgs_ns)]
    assert _outlier_family_note(ordered) is None


@given(
    base_ns=st.floats(min_value=1.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    multiple=st.floats(min_value=21.0, max_value=1e6, allow_nan=False, allow_infinity=False),
    n_peers=st.integers(min_value=2, max_value=8),
)
@FAST
def test_outlier_family_note_always_fires_when_one_family_dwarfs_its_peers(  # noqa: ANN201
    base_ns: float, multiple: float, n_peers: int
):
    # Generalizes the real-graph-trace regression above: ANY family whose
    # per-call average exceeds _OUTLIER_AVG_MULTIPLE times the median of
    # every other family's average must be flagged, regardless of the exact
    # kernel names or magnitudes involved.
    peers = [(f"peer_{i}", {"calls": 1, "total_ns": base_ns}) for i in range(n_peers)]
    dominant = ("dominant", {"calls": 1, "total_ns": base_ns * multiple})
    note = _outlier_family_note([dominant, *peers])
    assert note is not None
    assert "dominant" in note
    assert "compute.py profile" in note


def test_graphs_on_the_real_eager_trace_does_not_flag_degraded_attribution() -> None:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_graphs(_ns(str(_REAL_FIXTURE_ROOT / "eager")))
    out = buf.getvalue()

    assert "hipGraphLaunch calls: 0" in out
    assert "DEGRADED" not in out
    assert "should be reliable" in out


def test_graphs_on_the_real_graph_trace_flags_degraded_attribution() -> None:
    """The real ``hipGraphLaunch`` call (Correlation_Id 3945805 in the source
    capture) replays 6 kernels; the analyzer must detect and flag this.
    """
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_graphs(_ns(str(_REAL_FIXTURE_ROOT / "graph")))
    out = buf.getvalue()

    assert "hipGraphLaunch calls: 1" in out
    assert "DEGRADED" in out
    assert "matched kernels (by Correlation_Id): 1" in out


def test_kernels_on_the_real_eager_trace_matches_the_hand_computed_total() -> None:
    """Cross-check against the sum of (End - Start) for every sampled row."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cmd_kernels(_ns(str(_REAL_FIXTURE_ROOT / "eager"), top=20))
    out = buf.getvalue()

    assert "Total kernel launches: 14" in out
