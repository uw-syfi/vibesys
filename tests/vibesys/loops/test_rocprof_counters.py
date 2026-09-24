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
import io
import json
from pathlib import Path

import pytest
from resources.profilers.rocprof.counters import (
    ARCH_ALIASES,
    CANDIDATES,
    COUNTER_SETS,
    PEAK_SPECS,
    CounterSet,
    KernelAgg,
    _aggregate_by_kernel,
    _filter_kernels,
    _hbm_bytes,
    _load_counter_rows,
    _load_kernel_trace_durations,
    _lookup,
    _row_from_mapping,
    _walk_json_records,
    cmd_list_sets,
    cmd_plan,
    cmd_report,
    cmd_triage,
    derive_metrics,
    normalize_arch,
    resource_occupancy,
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
