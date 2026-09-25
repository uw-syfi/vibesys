"""Tests for the rocprof ATT capture planning and hotspot analysis toolkit.

The ``resources.profilers.rocprof.att`` import below resolves at runtime
because pytest's ``pythonpath = ["."]`` setting (pyproject.toml) puts the repo
root on ``sys.path``, and statically because the repo root is listed in
``[tool.ty.environment] root`` -- the same setup ``test_profiler.py``
documents for the nsys toolkit.

``fixtures/rocprof/att/ui_output_agent_123_dispatch_1/code.json`` is a small,
hand-built rocprofv3 ATT decoder output shaped to the documented row schema
(real MI210 captures were still being collected while this toolkit was
originally written).

``fixtures/rocprof/att_real/ui_output_agent_14537_dispatch_1/code.json`` is a
trimmed, path-sanitized copy of a **real** rocprofv3 ATT decoder output: MI210
(gfx90a), ROCm 7.2.0, decoder release 0.1.6, captured from a from-scratch
``axpy`` HIP kernel (`--att --att-target-cu 1 --att-buffer-size 67108864
--att-library-path <dir>`, see `att.py`'s module docstring). It confirms
``load_instructions``/``cmd_hotspots`` against the decoder's actual output
shape and its self-documented ``header`` field, not just the docs-derived
hand-built fixture above. Only the `Source` column's cluster-local build path
was rewritten to a portable `/src/...` path; every other field is verbatim.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof.att import (
    _DEFAULT_HEADER_COLUMNS,
    _HEADER_FIELD_MAP,
    STALL_CATEGORIES,
    AttOutputNotFoundError,
    Instruction,
    _find_code_json,
    _instruction_from_row,
    _parse_header,
    _stall_category,
    aggregate_by_source,
    cmd_hotspots,
    cmd_plan,
    load_instructions,
    stall_category_totals,
)
from resources.profilers.rocprof.att import (
    main as att_main,
)
from tests.vibesys.loops.rocprof_strategies import FAST, FEWER, buffer_size_input

_FIXTURES = Path(__file__).parent / "fixtures" / "rocprof"


def _run(fn, **kwargs) -> str:  # noqa: ANN001, ANN003  # LW-910204; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; this **kwargs parameter's type is intentionally left loose; annotating it now is separate cleanup work
    ns = argparse.Namespace(**kwargs)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(ns)
    return buf.getvalue()


def test_stall_category_classifies_common_isa_mnemonics():  # noqa: ANN201  # LW-910205; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    assert _stall_category("buffer_load_dwordx4 v[4:7], v0, s[8:11], 0 offen") == "VMEM-load"
    assert _stall_category("s_waitcnt vmcnt(0)") == "VMEM-wait"
    assert _stall_category("s_waitcnt lgkmcnt(0)") == "LDS/SMEM-wait"
    assert _stall_category("ds_write_b128 v0, v[8:11]") == "LDS"
    assert _stall_category("v_mfma_f32_16x16x16_f16 a[0:3], v[0:1], v[2:3], a[0:3]") == "MFMA/FMA"
    assert _stall_category("s_load_dwordx4 s[4:7], s[0:1], 0x0") == "SMEM"
    assert _stall_category("v_add_f32 v0, v1, v2") == "other"


def test_stall_categories_cover_every_declared_category_once_reachable():  # noqa: ANN201  # LW-910206; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    categories = {c for _, c in STALL_CATEGORIES}
    assert {
        "barrier",
        "VMEM-wait",
        "VMEM-load",
        "VMEM-store",
        "LDS",
        "SMEM",
        "MFMA/FMA",
    } <= categories


def test_instruction_from_row_skips_rows_with_zero_or_missing_pc_index():  # noqa: ANN201  # LW-910207; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    assert _instruction_from_row(["s_endpgm", "", 0, "", "", 1, 1, 10, 0, 10]) is None
    assert _instruction_from_row(["short", "", 1]) is None


def test_instruction_from_row_parses_a_full_row():  # noqa: ANN201  # LW-910208; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    inst = _instruction_from_row(
        ["buffer_load_dwordx4 v0", "", 3, "/k.py:38", "", 4108, 200, 9000, 8500, 500]
    )
    assert inst is not None
    assert inst.pc_index == 3
    assert inst.source_loc == "/k.py:38"
    assert inst.stall_cycles == 8500
    assert inst.category == "VMEM-load"
    assert inst.stall_pct == pytest.approx(8500 / 9000 * 100)


def test_instruction_stall_pct_is_zero_without_total_cycles():  # noqa: ANN201  # LW-910209; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    inst = Instruction(
        asm="nop",
        pc_index=1,
        source_loc="",
        pc_addr=0,
        exec_count=0,
        total_cycles=0,
        stall_cycles=0,
        idle_cycles=0,
    )
    assert inst.stall_pct == 0.0


def test_find_code_json_prefers_a_direct_file():  # noqa: ANN201  # LW-910210; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    path = _find_code_json(_FIXTURES / "att" / "ui_output_agent_123_dispatch_1")
    assert path.name == "code.json"


def test_find_code_json_descends_one_level_into_a_dispatch_dir():  # noqa: ANN201  # LW-910211; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    path = _find_code_json(_FIXTURES / "att")
    assert path.parent.name == "ui_output_agent_123_dispatch_1"


def test_find_code_json_raises_a_clear_error_when_absent(tmp_path: Path):  # noqa: ANN201  # LW-910212; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    with pytest.raises(AttOutputNotFoundError, match=r"no code\.json found"):
        _find_code_json(tmp_path)


def test_find_code_json_raises_when_multiple_dispatch_dirs_are_ambiguous(tmp_path: Path):  # noqa: ANN201  # LW-910213; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    for name in ("ui_output_agent_1_dispatch_1", "ui_output_agent_1_dispatch_2"):
        d = tmp_path / name
        d.mkdir()
        (d / "code.json").write_text("{}")
    with pytest.raises(AttOutputNotFoundError, match="multiple dispatch dirs"):
        _find_code_json(tmp_path)


def test_load_instructions_parses_the_fixture():  # noqa: ANN201  # LW-910214; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    instructions = load_instructions(_FIXTURES / "att" / "ui_output_agent_123_dispatch_1")
    # The s_endpgm row (pc_index=0) is dropped.
    assert len(instructions) == 5
    assert {i.category for i in instructions} == {
        "SMEM",
        "MFMA/FMA",
        "VMEM-load",
        "VMEM-wait",
        "LDS",
    }


def test_load_instructions_parses_real_mi210_att_decoder_output():  # noqa: ANN201  # LW-910215; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    """Regression test against real rocprofv3/rocprof-trace-decoder output (see module
    docstring), not just the docs-derived hand-built fixture: confirms the decoder's own
    ``header`` field ("ISA, _, LineNumber, Source, Codeobj, Vaddr, Hit, Latency, Stall, Idle")
    lines up with att.py's positional parsing, and that column 9 is idle cycles, not issue
    cycles (the docs-only guess this toolkit originally shipped with)."""
    instructions = load_instructions(_FIXTURES / "att_real" / "ui_output_agent_14537_dispatch_1")
    # 26 rows total; the leading "; <mangled name>" comment row (pc_index=0) is dropped.
    assert len(instructions) == 25
    first = instructions[0]
    assert first.asm == "s_load_dword s2, s[4:5], 0x24"
    assert first.pc_index == 1
    assert first.stall_cycles == 46468
    assert first.idle_cycles == 30636
    assert first.category == "SMEM"
    real_source_insns = [i for i in instructions if i.source_loc.startswith("/src/")]
    assert real_source_insns  # DWARF-mapped instructions carry the compiled kernel's own source
    assert all(i.source_loc != "<unknown>" for i in instructions)


def test_cmd_hotspots_on_real_att_data_ranks_the_actual_dominant_stall():  # noqa: ANN201  # LW-910216; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    out = _run(cmd_hotspots, dispatch_dir=str(_FIXTURES / "att_real"), top=3)
    # The real capture is dominated by a single s_waitcnt vmcnt(0) waiting on a global load.
    assert "VMEM-wait" in out
    assert "s_waitcnt vmcnt(0)" in out
    assert "/src/tiny_kernel.cpp:8" in out


def test_load_instructions_raises_on_malformed_json(tmp_path: Path):  # noqa: ANN201  # LW-910217; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    (tmp_path / "code.json").write_text("{not valid json")
    with pytest.raises(AttOutputNotFoundError, match="could not parse"):
        load_instructions(tmp_path)


def test_load_instructions_raises_without_a_code_list(tmp_path: Path):  # noqa: ANN201  # LW-910218; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    (tmp_path / "code.json").write_text(json.dumps({"not_code": []}))
    with pytest.raises(AttOutputNotFoundError, match="expected top-level 'code' list"):
        load_instructions(tmp_path)


def test_aggregate_by_source_sums_stall_cycles_per_line():  # noqa: ANN201  # LW-910219; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    instructions = load_instructions(_FIXTURES / "att" / "ui_output_agent_123_dispatch_1")
    hotspots = aggregate_by_source(instructions)
    assert hotspots[0].source_loc == "/kernels/attn.py:38"
    assert hotspots[0].total_stall_cycles == 8500
    assert hotspots[0].dominant_category == "VMEM-load"


def test_stall_category_totals_ranks_vmem_load_and_wait_highest():  # noqa: ANN201  # LW-910220; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    instructions = load_instructions(_FIXTURES / "att" / "ui_output_agent_123_dispatch_1")
    totals = dict(stall_category_totals(instructions))
    assert totals["VMEM-load"] == 8500
    assert totals["VMEM-wait"] == 8400
    assert list(dict(stall_category_totals(instructions))) == sorted(
        totals, key=lambda k: totals[k], reverse=True
    )


def test_cmd_hotspots_prints_category_totals_and_top_instructions():  # noqa: ANN201  # LW-910221; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    out = _run(cmd_hotspots, dispatch_dir=str(_FIXTURES / "att"), top=5)
    assert "Stall category totals" in out
    assert "VMEM-load" in out
    assert "Top 5 instructions by stall cycles" in out
    assert "/kernels/attn.py:38" in out
    assert "Top 5 source lines by aggregated stall cycles" in out


def test_cmd_hotspots_reports_a_clean_error_for_a_missing_dispatch_dir(tmp_path: Path):  # noqa: ANN201  # LW-910222; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    missing = tmp_path / "does_not_exist"
    with pytest.raises(SystemExit, match="not a directory"):
        _run(cmd_hotspots, dispatch_dir=str(missing), top=5)


def test_cmd_hotspots_reports_a_clean_error_when_decoder_output_is_absent(tmp_path: Path):  # noqa: ANN201  # LW-910223; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    with pytest.raises(SystemExit, match=r"no code\.json found"):
        _run(cmd_hotspots, dispatch_dir=str(tmp_path), top=5)


def test_cmd_plan_prints_a_working_rocprofv3_command_line():  # noqa: ANN201  # LW-910224; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    out = _run(
        cmd_plan,
        arch="gfx90a",
        kernel="flash_attn.*",
        target_cu=1,
        buffer_size=67_108_864,
        se_mask="0x1",
        simd_select="0xf",
        iteration_range=None,
        out_dir="rocprof_att",
        decoder_lib_dir="/opt/rocm-7.2.0/lib/att_decoder",
        script_path="rocprof_att.sh",
        write=False,
        command=["--", "python", "bench.py"],
    )
    assert "--kernel-include-regex 'flash_attn.*'" in out
    assert "--att " in out
    assert "--att-target-cu 1" in out
    # ATT is plain rocprofv3 CLI flags -- no `-i <job.yaml>` config path.
    assert "-i " not in out
    assert "--att-buffer-size 67108864" in out
    assert "--att-library-path /opt/rocm-7.2.0/lib/att_decoder" in out
    assert "rocprofv3" in out
    assert out.count("-- python bench.py") == 1  # the leading -- must not be duplicated
    assert "rocprof-trace-decoder" in out
    assert "PLAIN DECIMAL INTEGER" in out


def test_cmd_plan_without_a_command_prints_a_placeholder():  # noqa: ANN201  # LW-910225; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    out = _run(
        cmd_plan,
        arch="gfx90a",
        kernel="flash_attn.*",
        target_cu=1,
        buffer_size=67_108_864,
        se_mask="0x1",
        simd_select="0xf",
        iteration_range=None,
        out_dir="rocprof_att",
        decoder_lib_dir=None,
        script_path="rocprof_att.sh",
        write=False,
        command=[],
    )
    assert "<your_command_and_args>" in out
    assert "<dir containing librocprof-trace-decoder.so>" in out


def test_cmd_plan_includes_an_iteration_range_when_given():  # noqa: ANN201  # LW-910226; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    out = _run(
        cmd_plan,
        arch="gfx90a",
        kernel="gemm.*",
        target_cu=1,
        buffer_size=67_108_864,
        se_mask="0x1",
        simd_select="0xf",
        iteration_range=["2", "3", "4"],
        out_dir="rocprof_att",
        decoder_lib_dir="/opt/rocm/lib",
        script_path="rocprof_att.sh",
        write=False,
        command=[],
    )
    assert "--kernel-iteration-range 2 3 4" in out


def test_cmd_plan_can_write_the_command_to_a_script(tmp_path: Path):  # noqa: ANN201  # LW-910227; this function's return type is intentionally left loose; annotating it now is separate cleanup work
    script_path = tmp_path / "job.sh"
    _run(
        cmd_plan,
        arch="gfx942",
        kernel="gemm.*",
        target_cu=2,
        buffer_size=134_217_728,
        se_mask="0x1",
        simd_select="0xf",
        iteration_range=None,
        out_dir=str(tmp_path / "out"),
        decoder_lib_dir="/opt/rocm/lib",
        script_path=str(script_path),
        write=True,
        command=[],
    )
    assert script_path.is_file()
    assert "--att-target-cu 2" in script_path.read_text()


# ---------------------------------------------------------------------------
# Property tests: code.json column resolution is name-based, not positional
#
# Regression context: att.py used to read `code.json`'s `code` rows by fixed
# position (row[9] == idle cycles). That happened to match the one real
# decoder capture on hand, but nothing verified the *header* -- if a decoder
# release ever reorders columns, positional reads would silently mismap data.
# `_instruction_from_row`/`load_instructions` now resolve columns by the
# header's own names (falling back to the documented order only when no
# header is present at all). These tests fuzz the column order and prove the
# parser tracks the header, not position -- and fails clearly, rather than
# guessing, when a header is present but missing a column it needs.
# ---------------------------------------------------------------------------

_COLUMN_VALUES: dict[str, object] = {
    "ISA": "s_nop",
    "_": 0,
    "LineNumber": 7,
    "Source": "/k.cpp:1",
    "Codeobj": 0,
    "Vaddr": 4096,
    "Hit": 3,
    "Latency": 100,
    "Stall": 40,
    "Idle": 25,
}


@given(order=st.permutations(range(len(_DEFAULT_HEADER_COLUMNS))))
@FAST
def test_load_instructions_resolves_columns_by_header_name_under_any_permutation(order):  # noqa: ANN001, ANN201  # LW-920208; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    columns = [_DEFAULT_HEADER_COLUMNS[i] for i in order]
    row = [_COLUMN_VALUES[c] for c in columns]

    indices = _parse_header(", ".join(columns))
    inst = _instruction_from_row(row, indices)

    assert inst is not None
    assert inst.asm == "s_nop"
    assert inst.pc_index == 7
    assert inst.source_loc == "/k.cpp:1"
    assert inst.pc_addr == 4096
    assert inst.exec_count == 3
    assert inst.total_cycles == 100
    assert inst.stall_cycles == 40
    assert inst.idle_cycles == 25


@given(renamed_col=st.sampled_from(sorted(_HEADER_FIELD_MAP)))
@FAST
def test_parse_header_fails_clearly_when_a_required_column_is_renamed_away(renamed_col):  # noqa: ANN001, ANN201  # LW-920209; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # Rename one required header column to something unrecognized -- every
    # other required column stays present and correct.
    columns = [c if c != renamed_col else "Unknown_Renamed_Column" for c in _DEFAULT_HEADER_COLUMNS]

    with pytest.raises(AttOutputNotFoundError, match="missing required column"):
        _parse_header(", ".join(columns))


def test_load_instructions_reads_the_header_field_from_a_real_code_json_file(tmp_path: Path):  # noqa: ANN201  # LW-920210; tracked migration debt from the pre-manifest ratchet scheme
    # End-to-end (through the filesystem) with a header order that does NOT
    # match the documented default -- proves `load_instructions` itself (not
    # just the in-memory helpers above) resolves columns by name.
    columns = list(reversed(_DEFAULT_HEADER_COLUMNS))
    row = [_COLUMN_VALUES[c] for c in columns]
    dispatch_dir = tmp_path / "ui_output_agent_1_dispatch_1"
    dispatch_dir.mkdir()
    (dispatch_dir / "code.json").write_text(
        json.dumps({"header": ", ".join(columns), "code": [row]})
    )

    instructions = load_instructions(dispatch_dir)

    assert len(instructions) == 1
    assert instructions[0].idle_cycles == 25
    assert instructions[0].stall_cycles == 40


def test_instruction_from_row_default_indices_still_match_the_documented_order():  # noqa: ANN201  # LW-920211; tracked migration debt from the pre-manifest ratchet scheme
    # `_instruction_from_row`'s default `indices` argument (used when callers
    # -- including the existing hand-built-fixture tests above -- pass no
    # explicit mapping) must still match the module's own documented
    # positional fallback order.
    row = [_COLUMN_VALUES[c] for c in _DEFAULT_HEADER_COLUMNS]
    inst = _instruction_from_row(row)
    assert inst is not None
    assert inst.idle_cycles == 25
    assert inst.stall_cycles == 40


# ---------------------------------------------------------------------------
# Property test: --att-buffer-size is always a plain integer byte count
# ---------------------------------------------------------------------------


@given(value=buffer_size_input())
@FEWER
def test_cmd_plan_buffer_size_is_always_a_plain_integer_or_a_clear_cli_error(value):  # noqa: ANN001, ANN201  # LW-920212; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    argv = ["plan", "--arch", "gfx90a", "--kernel", "x", "--buffer-size", str(value)]
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            att_main(argv)
    except SystemExit:
        # argparse's own `type=int` rejected a non-integer string ("64MB",
        # "1G") -- a clear CLI error, not a silently-broken command.
        return
    match = re.search(r"--att-buffer-size (\S+)", buf.getvalue())
    assert match is not None
    assert match.group(1).isdigit()
