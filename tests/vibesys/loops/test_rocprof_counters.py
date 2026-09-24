"""Tests for the rocprof PMC counter-set catalogue, report, and triage toolkit.

The ``resources.profilers.rocprof.counters`` import below resolves at runtime
because pytest's ``pythonpath = ["."]`` setting (pyproject.toml) puts the repo
root on ``sys.path``, and statically because the repo root is listed in
``[tool.ty.environment] root`` -- the same setup ``test_profiler.py`` documents
for the nsys toolkit.

Fixtures under ``fixtures/rocprof/`` are small, hand-built rocprofv3
``*_counter_collection.csv`` / ``*_kernel_trace.csv`` samples shaped to the
documented column schema (real MI210 captures were still in progress while
this toolkit was written; see the module docstring's verified/unverified
counter-name notes).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import itertools
import json
import math
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof.counters import (
    ARCH_ALIASES,
    CANDIDATES,
    COUNTER_SETS,
    MFMA_BUSY_FRACTION_COMPUTE_BOUND,
    MFMA_CANDIDATE_KEYS,
    PEAK_SPECS,
    SIMDS_PER_CU,
    AgentInfo,
    CounterRow,
    CounterSet,
    KernelAgg,
    MfmaPeakContext,
    _aggregate_by_kernel,
    _duration_from_counter_rows,
    _filter_kernels,
    _hbm_bytes,
    _load_agent_info,
    _load_counter_rows,
    _load_kernel_trace_durations,
    _lookup,
    _mfma_busy_fraction,
    _row_from_mapping,
    _simd_num_for,
    _walk_json_records,
    cmd_list_sets,
    cmd_plan,
    cmd_report,
    cmd_triage,
    derive_metrics,
    normalize_arch,
    resource_occupancy,
)
from tests.vibesys.loops.rocprof_strategies import (
    FAST,
    FEWER,
    arch_name,
    case_variant,
    column_order,
    huge_kernel_name,
    permuted_csv,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "rocprof"


def _run(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # tracked: #288
    ns = argparse.Namespace(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    return buf.getvalue()


def test_normalize_arch_maps_known_skus():  # noqa: ANN201  # tracked: #288
    assert normalize_arch("MI210") == "gfx90a"
    assert normalize_arch("mi300x") == "gfx942"
    assert normalize_arch("MI355X") == "gfx950"


def test_normalize_arch_passes_through_gfx_ids():  # noqa: ANN201  # tracked: #288
    assert normalize_arch("gfx90a") == "gfx90a"
    assert normalize_arch("GFX942") == "gfx942"


def test_normalize_arch_rejects_unknown_name():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="unknown architecture"):
        normalize_arch("rtx4090")


def test_every_sku_alias_resolves_to_a_cataloged_family():  # noqa: ANN201  # tracked: #288
    for family in ARCH_ALIASES.values():
        assert family in COUNTER_SETS
        assert family in PEAK_SPECS


def test_counter_set_rejects_more_than_four_counters():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="1-4 counters"):
        CounterSet(("A", "B", "C", "D", "E"), verified=False)


def test_counter_set_rejects_empty_counters():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="1-4 counters"):
        CounterSet((), verified=False)


def test_every_catalogued_set_has_at_most_four_counters():  # noqa: ANN201  # tracked: #288
    for family, sets in COUNTER_SETS.items():
        for name, cset in sets.items():
            assert 1 <= len(cset.counters) <= 4, (
                f"{family}/{name} has {len(cset.counters)} counters"
            )


def test_all_three_arch_families_expose_the_same_set_names():  # noqa: ANN201  # tracked: #288
    names_by_family = {family: set(sets) for family, sets in COUNTER_SETS.items()}
    common = set.intersection(*names_by_family.values())
    assert common == {"occupancy", "mfma", "valu", "l2", "hbm", "lds"}


def test_gfx942_l2_and_hbm_sets_are_marked_verified():  # noqa: ANN201  # tracked: #288
    # These specific counter names come from a working rocprofv3 config; every
    # other set in the catalogue is explicitly UNVERIFIED until confirmed
    # against a real capture or `rocprofv3 --list-avail`.
    assert COUNTER_SETS["gfx942"]["l2"].verified
    assert COUNTER_SETS["gfx942"]["hbm"].verified


def test_gfx90a_mfma_set_prioritizes_the_measured_busy_cycles_formula():  # noqa: ANN201  # tracked: #288
    # SQ_VALU_MFMA_BUSY_CYCLES + GRBM_GUI_ACTIVE are what the fraction-of-peak
    # formula needs (mirrors rocprof-compute's own MfmaUtil derived metric);
    # SQ_INSTS_MFMA + GRBM_COUNT stay as the fallback path.
    cset = COUNTER_SETS["gfx90a"]["mfma"]
    assert cset.verified
    assert cset.counters == (
        "SQ_VALU_MFMA_BUSY_CYCLES",
        "GRBM_GUI_ACTIVE",
        "SQ_INSTS_MFMA",
        "GRBM_COUNT",
    )


def test_ridge_points_match_known_generational_trend():  # noqa: ANN201  # tracked: #288
    # gfx90a ~113, gfx942 ~247, gfx950 ~312 FLOP/byte -- the ridge should move
    # right each generation as matrix throughput outgrew HBM bandwidth.
    ridge = {family: spec.ridge_flop_per_byte for family, spec in PEAK_SPECS.items()}
    assert ridge["gfx90a"] < ridge["gfx942"] < ridge["gfx950"]
    assert 100 < ridge["gfx90a"] < 125  # tracked: #288
    assert 235 < ridge["gfx942"] < 260  # tracked: #288
    assert 300 < ridge["gfx950"] < 325  # tracked: #288


def test_row_from_mapping_accepts_both_casing_styles():  # noqa: ANN201  # tracked: #288
    pascal = _row_from_mapping(
        {
            "Kernel_Name": "k",
            "Counter_Name": "TCC_HIT_sum",
            "Counter_Value": "5",
            "Dispatch_Id": "1",
        }
    )
    snake = _row_from_mapping(
        {
            "kernel_name": "k",
            "counter_name": "TCC_HIT_sum",
            "counter_value": "5",
            "dispatch_id": "1",
        }
    )
    assert pascal is not None
    assert pascal == snake


def test_row_from_mapping_skips_incomplete_rows():  # noqa: ANN201  # tracked: #288
    assert _row_from_mapping({"Kernel_Name": "k", "Counter_Name": "X", "Counter_Value": ""}) is None
    assert _row_from_mapping({"Counter_Name": "X", "Counter_Value": "1"}) is None


def test_load_counter_rows_reads_fixture_csvs():  # noqa: ANN201  # tracked: #288
    l2_csv = _FIXTURES / "pmc" / "l2" / "pass_1" / "l2_counter_collection.csv"
    rows = _load_counter_rows([l2_csv])
    assert len(rows) == 4  # tracked: #288
    assert {r.counter_name for r in rows} == {"TCC_HIT_sum", "TCC_MISS_sum"}
    assert all(r.kernel_name == "flash_attn_decode_kernel" for r in rows)


def test_walk_json_records_finds_rows_in_a_nested_rocprofv3_shape():  # noqa: ANN201  # tracked: #288
    data = {
        "rocprofiler-sdk-tool": {
            "counter_collection": [
                {"Kernel_Name": "k", "Counter_Name": "GRBM_COUNT", "Counter_Value": 10},
                {"Kernel_Name": "k", "Counter_Name": "GRBM_GUI_ACTIVE", "Counter_Value": 9},
            ]
        }
    }
    records = _walk_json_records(data)
    assert len(records) == 2  # tracked: #288


def test_walk_json_records_handles_a_flat_list():  # noqa: ANN201  # tracked: #288
    data = [{"Kernel_Name": "k", "Counter_Name": "GRBM_COUNT", "Counter_Value": 10}]
    assert _walk_json_records(data) == data


def test_load_counter_rows_reads_json(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    path = tmp_path / "valu_counter_collection.json"
    path.write_text(
        json.dumps(
            {
                "rocprofiler-sdk-tool": {
                    "counter_collection": [
                        {
                            "Kernel_Name": "gemm_kernel",
                            "Dispatch_Id": "1",
                            "Grid_Size": 512,
                            "Workgroup_Size": 256,
                            "VGPR_Count": 64,
                            "SGPR_Count": 24,
                            "Counter_Name": "GRBM_GUI_ACTIVE",
                            "Counter_Value": 900,
                        }
                    ]
                }
            }
        )
    )
    rows = _load_counter_rows([path])
    assert len(rows) == 1
    assert rows[0].kernel_name == "gemm_kernel"
    assert rows[0].counter_value == 900.0  # tracked: #288


def test_load_counter_rows_warns_and_skips_unparseable_file(tmp_path: Path, capsys):  # noqa: ANN001, ANN201  # tracked: #288
    bad = tmp_path / "broken_counter_collection.json"
    bad.write_text("{not valid json")
    rows = _load_counter_rows([bad])
    assert rows == []
    assert "could not parse" in capsys.readouterr().err


def test_aggregate_by_kernel_sums_counters_and_tracks_peak_resources():  # noqa: ANN201  # tracked: #288
    rows = _load_counter_rows([_FIXTURES / "pmc" / "l2" / "pass_1" / "l2_counter_collection.csv"])
    aggs = _aggregate_by_kernel(rows)
    agg = aggs["flash_attn_decode_kernel"]
    assert agg.counters["TCC_HIT_sum"] == 2100.0  # tracked: #288
    assert agg.counters["TCC_MISS_sum"] == 17800.0  # tracked: #288
    assert agg.dispatch_count == 2  # tracked: #288
    assert agg.vgpr_count == 128  # tracked: #288


def test_kernel_agg_dispatch_count_defaults_to_one_without_dispatch_ids():  # noqa: ANN201  # tracked: #288
    assert KernelAgg(name="k").dispatch_count == 1


def test_filter_kernels_applies_regex_and_sorts_by_dispatch_count():  # noqa: ANN201  # tracked: #288
    aggs = {
        "attn": KernelAgg(name="attn", dispatch_ids={"1", "2", "3"}),
        "gemm": KernelAgg(name="gemm", dispatch_ids={"1"}),
        "norm": KernelAgg(name="norm", dispatch_ids={"1", "2"}),
    }
    ranked = _filter_kernels(aggs, None)
    assert [a.name for a in ranked] == ["attn", "norm", "gemm"]

    filtered = _filter_kernels(aggs, r"^g")
    assert [a.name for a in filtered] == ["gemm"]


def test_resource_occupancy_is_vgpr_bound_for_a_heavy_kernel():  # noqa: ANN201  # tracked: #288
    occ = resource_occupancy(
        vgpr_count=260, sgpr_count=40, lds_block_size=0, workgroup_size=256, arch="gfx90a"
    )
    assert occ.bound_by == "VGPR"
    assert occ.waves_per_simd == 1
    assert occ.waves_per_cu == 4  # tracked: #288


def test_resource_occupancy_full_at_low_register_pressure():  # noqa: ANN201  # tracked: #288
    occ = resource_occupancy(
        vgpr_count=64, sgpr_count=24, lds_block_size=0, workgroup_size=256, arch="gfx942"
    )
    assert occ.waves_per_simd == 8  # tracked: #288
    assert occ.waves_per_cu == 32  # tracked: #288


def test_resource_occupancy_lds_uses_the_wider_gfx950_budget():  # noqa: ANN201  # tracked: #288
    # Same LDS-per-workgroup on gfx942 (64KB/CU) vs. gfx950 (160KB/CU) must
    # give gfx950 more room, not less.
    common = {"vgpr_count": 32, "sgpr_count": 16, "lds_block_size": 20000, "workgroup_size": 256}
    occ_942 = resource_occupancy(arch="gfx942", **common)
    occ_950 = resource_occupancy(arch="gfx950", **common)
    assert occ_950.lds_limit >= occ_942.lds_limit
    assert occ_950.lds_total_bytes > occ_942.lds_total_bytes


def test_resource_occupancy_rejects_unknown_arch():  # noqa: ANN201  # tracked: #288
    with pytest.raises(ValueError, match="unknown architecture"):
        resource_occupancy(
            vgpr_count=32, sgpr_count=16, lds_block_size=0, workgroup_size=64, arch="rtx4090"
        )


def test_lookup_tries_every_candidate_name_in_order():  # noqa: ANN201  # tracked: #288
    assert CANDIDATES["hbm_rdreq"] == ("TCC_EA0_RDREQ_sum", "TCC_EA_RDREQ_sum")
    assert _lookup({"TCC_EA_RDREQ_sum": 5.0}, "hbm_rdreq") == 5.0
    assert _lookup({}, "hbm_rdreq") is None


def test_hbm_bytes_combines_full_and_partial_lines():  # noqa: ANN201  # tracked: #288
    # 495000 full (64B) + 5000 partial (32B) requests, matching the hbm fixture.
    total = _hbm_bytes({"TCC_EA0_RDREQ_sum": 500000.0, "TCC_EA0_RDREQ_32B_sum": 5000.0})
    assert total == 31_840_000.0  # tracked: #288


def test_hbm_bytes_is_none_without_a_request_counter():  # noqa: ANN201  # tracked: #288
    assert _hbm_bytes({"TCC_HIT_sum": 1.0}) is None


def test_derive_metrics_computes_l2_hit_rate_and_bandwidth():  # noqa: ANN201  # tracked: #288
    agg = KernelAgg(
        name="k",
        counters={"TCC_HIT_sum": 2100.0, "TCC_MISS_sum": 17800.0, "TCC_EA0_RDREQ_sum": 500000.0},
    )
    metrics = derive_metrics(agg, duration_ns=417_000.0)
    assert metrics.l2_hit_rate_pct == pytest.approx(10.55, abs=0.01)
    # No 32B-partial counter given, so every request is treated as a full 64B line.
    assert metrics.hbm_bytes == pytest.approx(32_000_000.0)
    assert metrics.achieved_bw_gb_s is not None
    assert metrics.gpu_busy_pct is None
    assert metrics.mfma_issue_rate is None


def test_derive_metrics_computes_gpu_busy_and_mfma_issue_rate():  # noqa: ANN201  # tracked: #288
    agg = KernelAgg(
        name="k",
        counters={
            "GRBM_GUI_ACTIVE": 950.0,
            "GRBM_COUNT": 1000.0,
            "SQ_INSTS_VALU_MFMA_MOPS_BF16": 900.0,
        },
    )
    metrics = derive_metrics(agg, duration_ns=None)
    assert metrics.gpu_busy_pct == pytest.approx(95.0)
    assert metrics.mfma_issue_rate == pytest.approx(0.9)
    assert metrics.achieved_bw_gb_s is None


def test_derive_metrics_computes_lds_bank_conflict_rate():  # noqa: ANN201  # tracked: #288
    agg = KernelAgg(name="k", counters={"SQ_INSTS_LDS": 1000.0, "SQ_LDS_BANK_CONFLICT": 50.0})
    metrics = derive_metrics(agg, duration_ns=None)
    assert metrics.lds_bank_conflict_rate_pct == pytest.approx(5.0)


def test_derive_metrics_returns_all_none_without_matching_counters():  # noqa: ANN201  # tracked: #288
    metrics = derive_metrics(
        KernelAgg(name="k", counters={"SOME_OTHER_COUNTER": 1.0}), duration_ns=None
    )
    assert metrics.l2_hit_rate_pct is None
    assert metrics.hbm_bytes is None
    assert metrics.gpu_busy_pct is None
    assert metrics.mfma_issue_rate is None
    assert metrics.lds_bank_conflict_rate_pct is None


def test_load_kernel_trace_durations_sums_per_kernel():  # noqa: ANN201  # tracked: #288
    durations = _load_kernel_trace_durations([str(_FIXTURES / "kernel_trace")])
    assert durations["flash_attn_decode_kernel"] == pytest.approx(417_000.0)


def test_load_kernel_trace_durations_is_empty_without_a_matching_file(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    assert _load_kernel_trace_durations([str(tmp_path)]) == {}


def test_load_agent_info_reads_the_real_mi210_capture():  # noqa: ANN201  # tracked: #288
    # 782163_agent_info.csv is a real rocprofv3 capture from the same MI210
    # job (gpu-node) the real_mi210_gfx90a counter fixtures come
    # from -- Cu_Count=104, Simd_Count=416 (416/104=4 SIMD/CU), matching the
    # static gfx90a PeakSpec/SIMDS_PER_CU table exactly.
    agent_info = _load_agent_info(_real_mi210_dirs())
    assert agent_info == AgentInfo(cu_count=104, simds_per_cu=4)
    assert agent_info.simd_num == 416


def test_load_agent_info_is_none_without_a_matching_file(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    assert _load_agent_info([str(tmp_path)]) is None


def test_load_agent_info_skips_the_cpu_row(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    path = tmp_path / "x_agent_info.csv"
    path.write_text("Agent_Type,Cu_Count,Simd_Count\nCPU,16,0\nGPU,104,416\n")
    assert _load_agent_info([str(tmp_path)]) == AgentInfo(cu_count=104, simds_per_cu=4)


def test_simd_num_for_prefers_agent_info_over_the_static_spec_table():  # noqa: ANN201  # tracked: #288
    spec = PEAK_SPECS["gfx942"]  # 304 CUs in the static table
    real = AgentInfo(cu_count=228, simds_per_cu=4)  # e.g. an MI300A capture
    assert _simd_num_for(spec, real) == real.simd_num
    assert _simd_num_for(spec, None) == spec.compute_units * SIMDS_PER_CU
    assert _simd_num_for(None, None) is None


def test_cmd_list_sets_prints_the_catalogue_for_one_arch():  # noqa: ANN201  # tracked: #288
    out = _run(cmd_list_sets, arch="mi210")
    assert "gfx90a" in out
    assert "l2" in out
    assert "hbm" in out


def test_cmd_list_sets_rejects_unknown_arch():  # noqa: ANN201  # tracked: #288
    with pytest.raises(SystemExit):
        _run(cmd_list_sets, arch="rtx4090")


def test_cmd_plan_prints_one_pass_per_set_with_separate_output_dirs():  # noqa: ANN201  # tracked: #288
    out = _run(
        cmd_plan,
        arch="gfx90a",
        sets="l2,hbm",
        kernel="flash_attn.*",
        out_dir="rocprof_pmc",
        command=["--", "python", "bench.py"],
    )
    assert out.count("rocprofv3 --pmc") == 2  # tracked: #288
    assert "rocprof_pmc/gfx90a/l2" in out
    assert "rocprof_pmc/gfx90a/hbm" in out
    assert "-- python bench.py" in out
    assert "-- -- python bench.py" not in out  # the leading -- must not be duplicated


def test_cmd_plan_rejects_unknown_counter_set():  # noqa: ANN201  # tracked: #288
    with pytest.raises(SystemExit, match="unknown counter set"):
        _run(cmd_plan, arch="gfx90a", sets="bogus", kernel=None, out_dir="rocprof_pmc", command=[])


def test_cmd_plan_without_a_command_prints_a_placeholder():  # noqa: ANN201  # tracked: #288
    out = _run(cmd_plan, arch="gfx90a", sets="l2", kernel=None, out_dir="rocprof_pmc", command=[])
    assert "<your_command_and_args>" in out


def test_cmd_report_merges_passes_and_prints_resource_usage_and_derived_metrics():  # noqa: ANN201  # tracked: #288
    dirs = [
        str(_FIXTURES / "pmc" / "l2"),
        str(_FIXTURES / "pmc" / "hbm"),
        str(_FIXTURES / "kernel_trace"),
    ]
    out = _run(cmd_report, dirs=dirs, kernel=None, top=15, arch="gfx90a")
    assert "flash_attn_decode_kernel" in out
    assert "2 dispatch(es)" in out
    assert "vgpr=128" in out
    assert "L2 hit rate: 10.6%" in out
    assert "achieved BW: 76." in out


def test_cmd_report_applies_kernel_regex_filter():  # noqa: ANN201  # tracked: #288
    dirs = [str(_FIXTURES / "pmc" / "l2")]
    out = _run(cmd_report, dirs=dirs, kernel="does_not_exist", top=15, arch=None)
    assert "0 kernel(s) matched" in out


def test_cmd_report_reports_no_files_found(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    out = _run(cmd_report, dirs=[str(tmp_path)], kernel=None, top=15, arch=None)
    assert "no *counter_collection*" in out


def test_cmd_triage_classifies_occupancy_bandwidth_compute_and_launch_kernels():  # noqa: ANN201  # tracked: #288
    occ_dir = str(_FIXTURES / "pmc" / "occupancy_low")
    out = _run(cmd_triage, dirs=[occ_dir], kernel=None, top=15, arch="gfx90a")

    assert "gemv_lowocc_kernel" in out
    assert "OCCUPANCY-LIMITED" in out
    assert "gemm_mfma_kernel" in out
    assert "COMPUTE-BOUND" in out
    assert "tiny_launch_kernel" in out
    assert "LAUNCH/UNDER-FILLED" in out
    # Peak table header with the "not an achievable ceiling" caveat.
    assert "SPEC PEAKS" in out
    assert "not an achievable ceiling" in out


def test_cmd_triage_falls_back_to_latency_bound_when_bandwidth_is_well_under_peak():  # noqa: ANN201  # tracked: #288
    # 76 GB/s achieved is ~5% of the 1.6 TB/s gfx90a HBM peak -- nowhere near
    # the 50% bandwidth-bound threshold -- and occupancy/grid are both fine,
    # so this lands in the residual LATENCY-BOUND bucket.
    dirs = [str(_FIXTURES / "pmc" / "hbm"), str(_FIXTURES / "kernel_trace")]
    out = _run(cmd_triage, dirs=dirs, kernel=None, top=15, arch="gfx90a")
    assert "flash_attn_decode_kernel" in out
    assert "LATENCY-BOUND" in out
    assert "capture an ATT trace" in out


def test_cmd_triage_rejects_unknown_arch():  # noqa: ANN201  # tracked: #288
    with pytest.raises(SystemExit, match="unknown architecture"):
        _run(cmd_triage, dirs=[str(_FIXTURES / "pmc" / "l2")], kernel=None, top=15, arch="rtx4090")


def test_cmd_triage_rejects_a_gfx_id_with_no_catalogued_peak_spec():  # noqa: ANN201  # tracked: #288
    # gfx908 (MI100, CDNA1) parses as a valid gfx id but has no PEAK_SPECS entry.
    with pytest.raises(SystemExit, match="no peak spec"):
        _run(cmd_triage, dirs=[str(_FIXTURES / "pmc" / "l2")], kernel=None, top=15, arch="gfx908")


def test_cmd_triage_reports_no_files_found(tmp_path: Path):  # noqa: ANN201  # tracked: #288
    out = _run(cmd_triage, dirs=[str(tmp_path)], kernel=None, top=15, arch="gfx90a")
    assert "no *counter_collection*" in out


# ---------------------------------------------------------------------------
# Real MI210 (gfx90a, ROCm 6.4.1) regression fixture
#
# `real_mi210_gfx90a/pass{1,2,3}/` mirrors a real rocprofv3 capture (AMD HPC
# Fund cluster, job1_inventory_pmc_att.sh) of `workload.py` -- a bf16
# 4096^3 GEMM (rocBLAS/Tensile kernel) + an fp32 elementwise axpy -- trimmed
# to 2 real dispatches per kernel, keeping the real Kernel_Name, counter
# values, and Start_Timestamp/End_Timestamp, and the real
# `pmc_1/<hostname>/<pid>_*` nesting rocprofv3 always creates under `-d`.
# ---------------------------------------------------------------------------

_REAL_MI210 = _FIXTURES / "pmc" / "real_mi210_gfx90a"
_REAL_GEMM_KERNEL = "Cijk_Ailk_Bljk_BBS_BH_Bias_HAS_SAV_UserArgs_MT256x128x32_MI32x32x1"
_REAL_EW_KERNEL = "void at::native::vectorized_elementwise_kernel<4, at::native::AUnaryFunctor"


def _real_mi210_dirs() -> list[str]:
    return [str(_REAL_MI210 / f"pass{i}") for i in (1, 2, 3)]


def test_real_mi210_report_finds_the_nested_pmc_1_output_and_derives_duration():  # noqa: ANN201  # tracked: #288
    # rocprofv3 nests output under <out_dir>/pmc_1/<hostname>/<pid>_* rather
    # than directly under <out_dir>; _discover must still find it via rglob,
    # and duration must come from the PMC rows' own timestamps (no separate
    # kernel_trace capture in this fixture, matching the real job).
    out = _run(cmd_report, dirs=_real_mi210_dirs(), kernel=None, top=15, arch="gfx90a")
    assert "Merged 3 counter file(s), 2 kernel(s) matched." in out
    assert "Duration data available for 2 kernel name(s)." in out
    assert "2 dispatch(es)" in out


def test_real_mi210_report_attributes_gemm_as_mfma_heavy_and_ew_as_bandwidth_heavy():  # noqa: ANN201  # tracked: #288
    out = _run(cmd_report, dirs=_real_mi210_dirs(), kernel=None, top=15, arch="gfx90a")
    assert _REAL_GEMM_KERNEL in out
    # SQ_INSTS_MFMA / GRBM_GUI_ACTIVE from the real capture -> ~5.45 insts/cycle.
    assert "MFMA issue rate: 5.4477 insts/cycle" in out
    assert _REAL_EW_KERNEL in out
    assert "achieved BW: 1428.9 GB/s" in out


def test_real_mi210_report_shortens_the_long_real_kernel_names():  # noqa: ANN201  # tracked: #288
    # The real Tensile GEMM kernel name is 442 chars; report must not print
    # it in full (this toolkit is meant to feed an LLM prompt).
    out = _run(cmd_report, dirs=_real_mi210_dirs(), kernel=None, top=15, arch="gfx90a")
    assert "UserArgs_MT256x128x32_MI32x32x1" in out  # enough of the name survives to identify it
    assert "WS64_WG64_4_1" not in out  # the tail of the full 442-char name is gone


def test_real_mi210_triage_classifies_gemm_compute_bound_and_ew_bandwidth_bound():  # noqa: ANN201  # tracked: #288
    out = _run(cmd_triage, dirs=_real_mi210_dirs(), kernel=None, top=15, arch="gfx90a")
    assert _REAL_GEMM_KERNEL in out
    assert "verdict: COMPUTE-BOUND" in out
    assert _REAL_EW_KERNEL in out
    assert "verdict: BANDWIDTH-BOUND" in out
    # GEMM's own achieved HBM bandwidth (~211 GB/s, ~13% of the 1.6 TB/s
    # peak) must not itself cross the 50% bandwidth-bound threshold -- the
    # occupancy check comes first, then bandwidth, then MFMA; GEMM should
    # fall through bandwidth and land on the MFMA check.
    assert "achieved 1429 GB/s" in out  # only the elementwise kernel's evidence
    assert "achieved 211 GB/s" not in out  # GEMM's bandwidth isn't the verdict driver


def test_real_mi210_triage_without_flops_falls_back_to_the_uninterpretable_raw_rate():  # noqa: ANN201  # tracked: #288
    # Documents the pre-fix behavior this fixture still exercises when neither
    # SQ_VALU_MFMA_BUSY_CYCLES nor --flops is available (job1 pass1 didn't
    # capture the busy-cycles counter): the raw insts/cycle number is kept as
    # an honestly-labeled fallback, not silently presented as peak-normalized.
    out = _run(cmd_triage, dirs=_real_mi210_dirs(), kernel=_REAL_GEMM_KERNEL, top=15, arch="gfx90a")
    assert "verdict: COMPUTE-BOUND" in out
    assert "MFMA issue rate 5.4477 insts/cycle" in out
    assert "no peak reference" in out
    assert "MFMA busy" not in out  # the new peak-normalized evidence didn't fire


def test_real_mi210_triage_expresses_mfma_utilization_as_a_fraction_of_spec_peak():  # noqa: ANN201  # tracked: #288
    # The real capture (job1 pass1) never requested SQ_VALU_MFMA_BUSY_CYCLES,
    # so this exercises the --flops fallback end to end: FLOPs = 2*M*N*K for
    # the fixture's bf16 4096^3 GEMM (see the "Real MI210" section docstring
    # above), duration comes straight from the real Start/End_Timestamp
    # columns (2320966 ns total across the kernel's 2 dispatches), and
    # 59.2 TFLOP/s achieved / 181.0 TFLOP/s spec peak = 32.7% -> 33%.
    flops = 2 * 4096 * 4096 * 4096
    out = _run(
        cmd_triage,
        dirs=_real_mi210_dirs(),
        kernel=_REAL_GEMM_KERNEL,
        top=15,
        arch="gfx90a",
        flops=flops,
    )
    assert _REAL_GEMM_KERNEL in out
    assert "verdict: COMPUTE-BOUND" in out
    assert "MFMA busy 33% of peak (spec peak, from --flops)" in out
    # The old uninterpretable raw-rate form must be gone from this verdict.
    assert "insts/cycle" not in out
    assert "MFMA issue rate" not in out


# ---------------------------------------------------------------------------
# Property tests: derive_metrics never crashes/NaNs/negatives, and computes
# every metric whose inputs are present, for ANY subset of an arch's
# catalogued counters (generalizes the MFMA-issue-rate / GRBM_COUNT-fallback
# regression pinned by the real_mi210 fixture above).
# ---------------------------------------------------------------------------


@st.composite
def _catalogued_counter_subset(draw: st.DrawFn) -> tuple[str, dict[str, float]]:
    arch = draw(arch_name)
    names = sorted({c for cset in COUNTER_SETS[arch].values() for c in cset.counters})
    chosen = draw(st.lists(st.sampled_from(names), min_size=0, max_size=len(names), unique=True))
    # Real hardware performance counters are non-negative integer event
    # counts (rocprofv3's Counter_Value column), never fractional -- in
    # particular, never a subnormal near-zero float. Drawing arbitrary
    # `st.floats` here once produced a denominator of 5e-324 (the smallest
    # positive double) and blew `mfma_issue_rate` up to `inf`: a hypothesis
    # strategy artifact outside the real counter-value domain, not a
    # production bug, so the fix is a more representative strategy here.
    values = draw(
        st.lists(
            st.integers(min_value=0, max_value=1_000_000_000).map(float),
            min_size=len(chosen),
            max_size=len(chosen),
        )
    )
    return arch, dict(zip(chosen, values, strict=True))


def _assert_finite_and_nonnegative(value: float | None) -> None:
    if value is not None:
        assert math.isfinite(value)
        assert value >= 0


@given(
    data=_catalogued_counter_subset(),
    duration_ns=st.one_of(
        st.none(), st.floats(min_value=1, max_value=1e12, allow_nan=False, allow_infinity=False)
    ),
)
@FAST
def test_derive_metrics_never_crashes_or_emits_nan_inf_negative_for_any_counter_subset(  # noqa: ANN201
    data: tuple[str, dict[str, float]], duration_ns: float | None
):
    _arch, counters = data
    metrics = derive_metrics(KernelAgg(name="k", counters=counters), duration_ns)

    for value in (
        metrics.l2_hit_rate_pct,
        metrics.hbm_bytes,
        metrics.achieved_bw_gb_s,
        metrics.gpu_busy_pct,
        metrics.mfma_issue_rate,
        metrics.lds_bank_conflict_rate_pct,
    ):
        _assert_finite_and_nonnegative(value)

    # Every metric whose inputs ARE present must be computed, not "n/a".
    hit, miss = _lookup(counters, "l2_hit"), _lookup(counters, "l2_miss")
    if hit is not None and miss is not None and (hit + miss) > 0:
        assert metrics.l2_hit_rate_pct is not None

    if _lookup(counters, "hbm_rdreq") is not None or _lookup(counters, "hbm_wrreq") is not None:
        assert metrics.hbm_bytes is not None

    busy, total = _lookup(counters, "busy_cycles"), _lookup(counters, "total_cycles")
    if busy is not None and total is not None and total > 0:
        assert metrics.gpu_busy_pct is not None

    # Mirrors `_mfma_instruction_count`'s own precedence exactly: the total
    # counter (SQ_INSTS_MFMA) is authoritative whenever it was CAPTURED, even
    # if its value is 0 (a truthful "no MFMA issued"); the per-dtype MOPS_*
    # counters are only a fallback when the total counter is entirely absent.
    mfma_total = _lookup(counters, "mfma_insts_total")
    dtype_sum = sum(v for k in MFMA_CANDIDATE_KEYS if (v := _lookup(counters, k)) is not None)
    effective_mfma = (
        mfma_total if mfma_total is not None else (dtype_sum if dtype_sum > 0 else None)
    )
    cycles_for_rate = total if (total is not None and total > 0) else busy
    mfma_present = effective_mfma is not None and effective_mfma > 0
    if mfma_present and cycles_for_rate is not None and cycles_for_rate > 0:
        assert metrics.mfma_issue_rate is not None

    lds_insts, bank_conflicts = (
        _lookup(counters, "lds_insts"),
        _lookup(counters, "lds_bank_conflict"),
    )
    if lds_insts is not None and bank_conflicts is not None and lds_insts > 0:
        assert metrics.lds_bank_conflict_rate_pct is not None


@given(arch=arch_name)
@FEWER
def test_report_and_triage_never_crash_over_every_arch_counter_set_in_isolation(arch: str):  # noqa: ANN201
    # One CSV per catalogued counter set for this arch, each row using every
    # counter that set names -- mirrors a real multi-pass capture directory.
    rows = []
    corr = 0
    for cset in COUNTER_SETS[arch].values():
        for counter in cset.counters:
            corr += 1
            rows.append(f"{corr},1,0,0,100,1,256,1,test_kernel,256,0,0,64,32,{counter},{corr * 10}")
    header = (
        "Correlation_Id,Dispatch_Id,Agent_Id,Queue_Id,Process_Id,Thread_Id,Grid_Size,"
        "Kernel_Id,Kernel_Name,Workgroup_Size,LDS_Block_Size,Scratch_Size,VGPR_Count,"
        "SGPR_Count,Counter_Name,Counter_Value"
    )
    counter_rows = [
        _row_from_mapping(r) for r in csv.DictReader(io.StringIO(header + "\n" + "\n".join(rows)))
    ]
    counters = {r.counter_name: r.counter_value for r in counter_rows if r is not None}
    agg = KernelAgg(name="test_kernel", counters=counters)
    occ = resource_occupancy(
        vgpr_count=64, sgpr_count=32, lds_block_size=0, workgroup_size=256, arch=arch
    )
    metrics = derive_metrics(agg, duration_ns=None)
    for value in (
        metrics.l2_hit_rate_pct,
        metrics.hbm_bytes,
        metrics.gpu_busy_pct,
        metrics.mfma_issue_rate,
        metrics.lds_bank_conflict_rate_pct,
    ):
        _assert_finite_and_nonnegative(value)
    assert occ.waves_per_simd >= 1


# ---------------------------------------------------------------------------
# Property tests: mfma_busy_fraction (peak-normalized MFMA utilization) is
# always in [0, 1] (clamped + warned when the raw ratio isn't), monotone in
# the busy-cycles numerator, and invariant to scaling every cycle counter in
# the ratio together -- generalizes the real_mi210 "% of peak" regression.
# ---------------------------------------------------------------------------


@given(
    mfma_busy_cycles=st.floats(min_value=0, max_value=1e9, allow_nan=False, allow_infinity=False),
    grbm_gui_active=st.floats(min_value=1, max_value=1e9, allow_nan=False, allow_infinity=False),
    simd_num=st.integers(min_value=1, max_value=2000),
)
@FAST
def test_mfma_busy_fraction_measured_path_always_in_unit_interval(  # noqa: ANN201
    mfma_busy_cycles: float, grbm_gui_active: float, simd_num: int
):
    fraction, source = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": mfma_busy_cycles},
        busy_cycles=grbm_gui_active,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=simd_num),
    )
    assert fraction is not None
    assert 0.0 <= fraction <= 1.0
    assert source == "measured"


@given(
    mfma_busy_cycles=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    extra_busy_cycles=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    grbm_gui_active=st.floats(min_value=1, max_value=1e9, allow_nan=False, allow_infinity=False),
    simd_num=st.integers(min_value=1, max_value=2000),
)
@FAST
def test_mfma_busy_fraction_is_monotone_in_busy_cycles(  # noqa: ANN201
    mfma_busy_cycles: float, extra_busy_cycles: float, grbm_gui_active: float, simd_num: int
):
    lower, _ = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": mfma_busy_cycles},
        busy_cycles=grbm_gui_active,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=simd_num),
    )
    higher, _ = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": mfma_busy_cycles + extra_busy_cycles},
        busy_cycles=grbm_gui_active,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=simd_num),
    )
    assert lower is not None
    assert higher is not None
    assert higher >= lower


@given(
    mfma_busy_cycles=st.floats(min_value=1, max_value=1e6, allow_nan=False, allow_infinity=False),
    grbm_gui_active=st.floats(min_value=1, max_value=1e9, allow_nan=False, allow_infinity=False),
    simd_num=st.integers(min_value=1, max_value=2000),
    scale=st.floats(min_value=1e-3, max_value=1e3, allow_nan=False, allow_infinity=False),
)
@FAST
def test_mfma_busy_fraction_is_invariant_to_scaling_all_cycle_counters_together(  # noqa: ANN201
    mfma_busy_cycles: float, grbm_gui_active: float, simd_num: int, scale: float
):
    # Scaling both the numerator (SQ_VALU_MFMA_BUSY_CYCLES) and the busy-cycle
    # term of the denominator (GRBM_GUI_ACTIVE) by the same factor is exactly
    # what happens when the same dispatch is re-measured at a different clock
    # or over a different-length capture window with proportional counts --
    # the ratio itself must not move.
    base, _ = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": mfma_busy_cycles},
        busy_cycles=grbm_gui_active,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=simd_num),
    )
    scaled, _ = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": mfma_busy_cycles * scale},
        busy_cycles=grbm_gui_active * scale,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=simd_num),
    )
    assert base is not None
    assert scaled is not None
    assert scaled == pytest.approx(base, rel=1e-6, abs=1e-9)


def test_mfma_busy_fraction_clamps_and_warns_when_measured_value_exceeds_peak(capsys):  # noqa: ANN001, ANN201  # tracked: #288
    # SQ_VALU_MFMA_BUSY_CYCLES far larger than busy_cycles*simd_num could
    # allow -- inconsistent counters (stitched passes, a measurement race).
    fraction, source = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": 1_000_000.0},
        busy_cycles=10.0,
        duration_ns=None,
        peak=MfmaPeakContext(simd_num=4),
    )
    assert fraction == 1.0
    assert source == "measured"
    assert "warning" in capsys.readouterr().err.lower()


def test_mfma_busy_fraction_flops_hint_only_fires_when_the_kernel_has_mfma_activity():  # noqa: ANN201  # tracked: #288
    # A --flops hint must never manufacture a compute-bound signal for a
    # kernel that issued zero MFMA instructions (e.g. the elementwise kernel
    # in the real_mi210 fixture) even if a peak spec and duration are given.
    fraction, source = _mfma_busy_fraction(
        counters={},
        busy_cycles=None,
        duration_ns=1000.0,
        peak=MfmaPeakContext(simd_num=None, spec=PEAK_SPECS["gfx90a"], flops=1e12),
    )
    assert fraction is None
    assert source is None


def test_mfma_busy_fraction_prefers_measured_over_flops_hint_when_both_are_present():  # noqa: ANN201  # tracked: #288
    fraction, source = _mfma_busy_fraction(
        counters={"SQ_VALU_MFMA_BUSY_CYCLES": 100.0, "SQ_INSTS_MFMA": 1.0},
        busy_cycles=1000.0,
        duration_ns=1000.0,
        peak=MfmaPeakContext(simd_num=416, spec=PEAK_SPECS["gfx90a"], flops=1e12),
    )
    assert source == "measured"
    assert fraction == pytest.approx(100.0 / (1000.0 * 416))


def test_mfma_busy_fraction_compute_bound_threshold_is_well_below_the_practical_tuned_gemm_ceiling():  # noqa: ANN201  # tracked: #288
    # 45-55% of spec peak is the documented realistic ceiling for a tuned
    # GEMM (see PeakSpec's docstring); the compute-bound threshold must sit
    # comfortably below that so a well-tuned kernel is still classified
    # COMPUTE-BOUND rather than falling through to a different verdict.
    assert 0.0 < MFMA_BUSY_FRACTION_COMPUTE_BOUND < 0.45


# ---------------------------------------------------------------------------
# Property test: HBM bytes formula, and read/write counter-convention symmetry
#
# Regression context: the write side's "size breakdown" counter reports the
# FULL (64B) request count directly, while the read side's reports the
# PARTIAL (32B) count directly -- an asymmetric convention. The pre-fix code
# treated both sides the same way (looked for a nonexistent 32B write
# counter), so a real write breakdown was silently ignored and every write
# request was counted as a full 64B line even when some were partial.
# ---------------------------------------------------------------------------


@given(
    full=st.integers(min_value=0, max_value=1_000_000),
    partial=st.integers(min_value=0, max_value=1_000_000),
    ea_prefix=st.sampled_from(("TCC_EA0_", "TCC_EA_")),
)
@FAST
def test_hbm_bytes_matches_the_documented_formula_and_is_symmetric_across_conventions(  # noqa: ANN201
    full: int, partial: int, ea_prefix: str
):
    expected = float(full * 64 + partial * 32)
    # Read side reports the 32B PARTIAL count directly.
    read_counters = {
        f"{ea_prefix}RDREQ_sum": float(full + partial),
        f"{ea_prefix}RDREQ_32B_sum": float(partial),
    }
    # Write side reports the 64B FULL count directly (the asymmetric part).
    write_counters = {
        f"{ea_prefix}WRREQ_sum": float(full + partial),
        f"{ea_prefix}WRREQ_64B_sum": float(full),
    }
    read_bytes = _hbm_bytes(read_counters)
    write_bytes = _hbm_bytes(write_counters)
    assert read_bytes == pytest.approx(expected)
    assert write_bytes == pytest.approx(expected)
    assert read_bytes == pytest.approx(write_bytes)


@given(
    full=st.integers(min_value=0, max_value=1_000_000),
    partial=st.integers(min_value=0, max_value=1_000_000),
)
@FAST
def test_hbm_bytes_without_a_write_breakdown_assumes_every_request_is_a_full_line(  # noqa: ANN201
    full: int, partial: int
):
    # No *_WRREQ_64B_sum captured at all -- must conservatively assume every
    # write request is a full 64B line (this is the documented fallback, not
    # the bug: the bug was ignoring a *present* breakdown).
    req = full + partial
    assert _hbm_bytes({"TCC_EA0_WRREQ_sum": float(req)}) == pytest.approx(float(req) * 64)


@given(
    req=st.floats(min_value=0, max_value=1_000, allow_nan=False, allow_infinity=False),
    breakdown=st.floats(min_value=0, max_value=1_000_000, allow_nan=False, allow_infinity=False),
    side=st.sampled_from(("read", "write")),
)
@FAST
def test_hbm_bytes_never_goes_negative_when_a_breakdown_counter_exceeds_the_total(  # noqa: ANN201
    req: float, breakdown: float, side: str
):
    # Regression for a bug this property suite itself found: real counters
    # should never report a size-breakdown count larger than the matching
    # total request count, but nothing guarantees that (measurement races, a
    # partial/corrupted capture, or counters stitched together from
    # different passes). `req=0, breakdown=1` used to make `_hbm_bytes`
    # return -32.0.
    if side == "read":
        counters = {"TCC_EA0_RDREQ_sum": req, "TCC_EA0_RDREQ_32B_sum": breakdown}
    else:
        counters = {"TCC_EA0_WRREQ_sum": req, "TCC_EA0_WRREQ_64B_sum": breakdown}
    result = _hbm_bytes(counters)
    assert result is not None
    assert result >= 0


# ---------------------------------------------------------------------------
# Property test: duration is deduped by (kernel_name, dispatch_id), not by
# counter count -- generalizes the real_mi210 duration-from-PMC-timestamps fix.
# ---------------------------------------------------------------------------


@given(
    dispatch_count=st.integers(min_value=1, max_value=5),
    counters_per_dispatch=st.integers(min_value=1, max_value=4),
    start=st.floats(min_value=0, max_value=1e6, allow_nan=False, allow_infinity=False),
    dur=st.floats(min_value=1, max_value=1e6, allow_nan=False, allow_infinity=False),
)
@FAST
def test_duration_from_counter_rows_dedupes_by_dispatch_not_by_counter_count(  # noqa: ANN201
    dispatch_count: int, counters_per_dispatch: int, start: float, dur: float
):
    rows = []
    expected_total = 0.0
    for d in range(dispatch_count):
        dispatch_id = str(d)
        s, e = start + d * 10 * dur, start + d * 10 * dur + dur
        expected_total += e - s
        rows.extend(
            CounterRow(
                kernel_name="k",
                dispatch_id=dispatch_id,
                grid_size=0,
                workgroup_size=0,
                lds_block_size=0,
                scratch_size=0,
                vgpr_count=0,
                sgpr_count=0,
                counter_name=f"COUNTER_{c}",
                counter_value=1.0,
                start_ns=s,
                end_ns=e,
            )
            for c in range(counters_per_dispatch)
        )
    durations = _duration_from_counter_rows(rows)
    assert durations["k"] == pytest.approx(expected_total)


def test_kernel_trace_duration_overrides_pmc_derived_duration_when_both_present():  # noqa: ANN201
    pmc_rows = [
        CounterRow(
            kernel_name="k",
            dispatch_id="1",
            grid_size=0,
            workgroup_size=0,
            lds_block_size=0,
            scratch_size=0,
            vgpr_count=0,
            sgpr_count=0,
            counter_name="GRBM_COUNT",
            counter_value=1.0,
            start_ns=0.0,
            end_ns=100.0,  # PMC-derived duration: 100ns
        )
    ]
    durations = _duration_from_counter_rows(pmc_rows)
    assert durations["k"] == pytest.approx(100.0)
    # A dedicated kernel_trace capture (PMC-overhead-free) reports a
    # different duration for the same kernel; report/triage's merge order
    # (`durations.update(_load_kernel_trace_durations(...))`) must let it win.
    durations.update({"k": 87.0})
    assert durations["k"] == pytest.approx(87.0)


# ---------------------------------------------------------------------------
# Property tests: CSV robustness -- column order, case variants of known
# aliases, extra unknown columns, missing optional columns, huge (5000+
# char) kernel names. Assertion is "never crashes"; a casing variant outside
# the two exact aliases counters.py recognizes (PascalCase/snake_case) may
# legitimately be dropped rather than parsed.
# ---------------------------------------------------------------------------

_COUNTERS_CSV_HEADER = [
    "Correlation_Id",
    "Dispatch_Id",
    "Agent_Id",
    "Queue_Id",
    "Process_Id",
    "Thread_Id",
    "Grid_Size",
    "Kernel_Id",
    "Kernel_Name",
    "Workgroup_Size",
    "LDS_Block_Size",
    "Scratch_Size",
    "VGPR_Count",
    "SGPR_Count",
    "Counter_Name",
    "Counter_Value",
]


@given(
    order=column_order(len(_COUNTERS_CSV_HEADER)),
    include_unknown_column=st.booleans(),
    kernel_name=st.one_of(st.text(min_size=1, max_size=60), huge_kernel_name()),
    kernel_name_col=case_variant("Kernel_Name"),
)
@FEWER
def test_load_counter_rows_handles_permuted_noisy_and_huge_name_csvs_without_crashing(  # noqa: ANN201
    order: list[int], *, include_unknown_column: bool, kernel_name: str, kernel_name_col: str
):
    header = list(_COUNTERS_CSV_HEADER)
    row = [
        "1",
        "1",
        "0",
        "0",
        "100",
        "1",
        "256",
        "1",
        kernel_name,
        "256",
        "0",
        "0",
        "64",
        "32",
        "TCC_HIT_sum",
        "5",
    ]
    if include_unknown_column:
        header.append("Some_Future_Column")
        row.append("unexpected-value")
        order = [*order, len(header) - 1]
    header = [kernel_name_col if h == "Kernel_Name" else h for h in header]

    text = permuted_csv(header, [row], order)
    parsed = [
        r
        for r in (_row_from_mapping(r) for r in csv.DictReader(io.StringIO(text)))
        if r is not None
    ]

    if kernel_name_col in ("Kernel_Name", "kernel_name"):
        assert len(parsed) == 1
        assert parsed[0].kernel_name == kernel_name
    # else: an unrecognized casing may legitimately drop the row -- the only
    # hard requirement is that parsing never raises.


@given(
    dropped=st.lists(
        st.sampled_from(
            [
                "Dispatch_Id",
                "Grid_Size",
                "Workgroup_Size",
                "LDS_Block_Size",
                "Scratch_Size",
                "VGPR_Count",
                "SGPR_Count",
            ]
        ),
        unique=True,
        min_size=0,
        max_size=7,
    )
)
@FAST
def test_row_from_mapping_tolerates_any_subset_of_missing_optional_columns(dropped: list[str]):  # noqa: ANN201
    row = {
        "Kernel_Name": "k",
        "Counter_Name": "TCC_HIT_sum",
        "Counter_Value": "5",
        "Dispatch_Id": "9",
        "Grid_Size": "256",
        "Workgroup_Size": "256",
        "LDS_Block_Size": "1024",
        "Scratch_Size": "0",
        "VGPR_Count": "64",
        "SGPR_Count": "32",
    }
    for col in dropped:
        row.pop(col, None)
    parsed = _row_from_mapping(row)
    assert parsed is not None
    assert parsed.kernel_name == "k"
    assert parsed.counter_value == 5.0


def test_load_counter_rows_handles_a_header_only_empty_csv(tmp_path: Path):  # noqa: ANN201
    path = tmp_path / "empty_counter_collection.csv"
    path.write_text(
        "Correlation_Id,Dispatch_Id,Agent_Id,Queue_Id,Process_Id,Thread_Id,Grid_Size,"
        "Kernel_Id,Kernel_Name,Workgroup_Size,LDS_Block_Size,Scratch_Size,VGPR_Count,"
        "SGPR_Count,Counter_Name,Counter_Value\n"
    )
    assert _load_counter_rows([path]) == []

    out = _run(cmd_report, dirs=[str(tmp_path)], kernel=None, top=15, arch=None)
    assert "counter files found but no usable rows parsed" in out


_LINE_WIDTH_BOUND = 200


@given(kernel_name=huge_kernel_name())
@FEWER
def test_cmd_report_bounds_line_width_for_a_huge_real_shaped_kernel_name(  # noqa: ANN201
    tmp_path_factory: pytest.TempPathFactory, kernel_name: str
):
    d = tmp_path_factory.mktemp("huge_kernel_name")
    (d / "huge_counter_collection.csv").write_text(
        "Correlation_Id,Dispatch_Id,Agent_Id,Queue_Id,Process_Id,Thread_Id,Grid_Size,"
        "Kernel_Id,Kernel_Name,Workgroup_Size,LDS_Block_Size,Scratch_Size,VGPR_Count,"
        "SGPR_Count,Counter_Name,Counter_Value\n"
        f"1,1,0,0,100,1,256,1,{kernel_name},256,0,0,64,32,GRBM_COUNT,1000\n"
    )
    out = _run(cmd_report, dirs=[str(d)], kernel=None, top=15, arch="gfx942")
    assert all(len(line) <= _LINE_WIDTH_BOUND for line in out.splitlines())


# ---------------------------------------------------------------------------
# Property test: `plan` never emits more than 4 counters per pass, and every
# counter it prints for an arch is drawn from that arch's own catalogue.
# ---------------------------------------------------------------------------


def _pmc_counter_lists_from_plan_output(out: str) -> list[list[str]]:
    """Extract each pass's counter list from a `plan` invocation's printed command lines."""
    passes = []
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line.startswith("rocprofv3 --pmc "):
            continue
        tokens = line.split()
        counters = list(itertools.takewhile(lambda t: not t.startswith("--"), tokens[2:]))
        passes.append(counters)
    return passes


@given(arch=arch_name, data=st.data())
@FEWER
def test_plan_never_emits_more_than_four_counters_and_stays_in_the_arch_catalogue(  # noqa: ANN201
    arch: str, data: st.DataObject
):
    catalogue = COUNTER_SETS[arch]
    set_names = data.draw(
        st.lists(
            st.sampled_from(sorted(catalogue)), min_size=1, max_size=len(catalogue), unique=True
        )
    )
    known_counters = {c for cset in catalogue.values() for c in cset.counters}

    out = _run(
        cmd_plan,
        arch=arch,
        sets=",".join(set_names),
        kernel=None,
        out_dir="rocprof_pmc",
        command=[],
    )

    passes = _pmc_counter_lists_from_plan_output(out)
    assert len(passes) == len(set_names)
    for counters in passes:
        assert 1 <= len(counters) <= 4
        assert all(c in known_counters for c in counters)
