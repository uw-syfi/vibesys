"""Tests for the callgrind attribution parser (example profiler capability).

``examples/differential-dataflow-cpu-bench/profiler/attribute_cpu.py`` turns
``callgrind_annotate`` output into a ranked, fixed-vocabulary component list that
drives the loop's bottleneck walk. The heavy part — running valgrind — is not
exercised here; instead we feed a captured, trimmed real ``callgrind_annotate``
sample (``fixtures/callgrind_annotate_sample.txt``) through the pure parsing +
classification + aggregation path and assert the ranking is stable and correct.

The module lives under ``examples/`` co-located with the analysis scripts, so it
is loaded by file path via ``importlib.util.spec_from_file_location``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_ATTR_PY = _REPO / "examples" / "differential-dataflow-cpu-bench" / "profiler" / "attribute_cpu.py"
_SAMPLE = Path(__file__).with_name("fixtures") / "callgrind_annotate_sample.txt"


def _load_module(name: str, path: Path):  # noqa: ANN202  # tracked: #288
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # attribute_cpu.py does `import workload` at import time and inserts its own
    # reference dir on sys.path, so no extra path juggling is needed here.
    parent = str(path.parent)
    inserted = False
    if parent not in sys.path:
        sys.path.insert(0, parent)
        inserted = True
    try:
        spec.loader.exec_module(module)
    finally:
        if inserted:
            sys.path.remove(parent)
    return module


@pytest.fixture(scope="module")
def attr():  # noqa: ANN201  # tracked: #288
    return _load_module("attribute_cpu_under_test", _ATTR_PY)


@pytest.fixture(scope="module")
def sample_text():  # noqa: ANN201  # tracked: #288
    return _SAMPLE.read_text()


def test_parse_annotate_skips_totals_and_headers(attr, sample_text):  # noqa: ANN001, ANN201  # tracked: #288
    rows = attr.parse_annotate(sample_text)
    assert rows, "expected some parsed rows"
    assert all("PROGRAM TOTALS" not in func for _, _, func in rows)
    for ir, file, func in rows:
        assert ir > 0
        assert file
        assert func


def test_object_annotation_is_stripped(attr, sample_text):  # noqa: ANN001, ANN201  # tracked: #288
    rows = attr.parse_annotate(sample_text)
    assert any("[" not in file and "[" not in func for _, file, func in rows)
    for _, file, func in rows:
        assert "[" not in file
        assert "]" not in func


def test_classify_covers_the_fixed_vocabulary(attr):  # noqa: ANN001, ANN201  # tracked: #288
    assert (
        attr.classify("engine/differential-dataflow/src/trace/cursor/cursor_list.rs", "foo")
        == "trace/cursor"
    )
    assert attr.classify("differential-dataflow/src/consolidation.rs", "foo") == "consolidation"
    assert (
        attr.classify("differential-dataflow/src/operators/reduce.rs", "foo") == "operators/reduce"
    )
    # Monomorphized copy filed under /rustc/: the mangled symbol's dd defining
    # path is authoritative, not the stdlib file.
    monomorphized = (
        "_RNvMs0_NtNtNtCseK9igXeK8qy_21differential_dataflow5trace15implementations"
        "13merge_batcher8merge_by"
    )
    assert (
        attr.classify("/rustc/abc/library/alloc/src/vec/mod.rs", monomorphized)
        == "trace/implementations"
    )
    assert attr.classify("./malloc/malloc.c", "_int_free") == "libc/malloc"
    assert (
        attr.classify("./string/x/memmove-vec-unaligned-erms.S", "__memcpy_avx_unaligned_erms")
        == "libc/mem"
    )
    assert attr.classify("/rustc/abc/library/core/src/num.rs", "plain_func") == "rust-stdlib"
    assert attr.classify("/home/x/.cargo/registry/rand-0.4.6/src/prng/isaac64.rs", "f") == "other"


def test_aggregate_ranks_by_ir_and_computes_pct(attr, sample_text):  # noqa: ANN001, ANN201  # tracked: #288
    rows = attr.parse_annotate(sample_text)
    total = sum(ir for ir, _, _ in rows)
    components = attr.aggregate(rows)

    assert sum(c["ir"] for c in components) == total
    keys = [(-c["ir"], c["component"]) for c in components]
    assert keys == sorted(keys)
    for c in components:
        assert c["pct"] == pytest.approx(round(c["ir"] / total * 100, 2))
        assert len(c["top_functions"]) <= 3
    assert components[0]["component"] == "trace/implementations"
    assert components[0]["top_functions"]
    names = {c["component"] for c in components}
    assert {"trace/implementations", "consolidation", "libc/malloc", "libc/mem"} <= names


def test_ranking_is_deterministic(attr, sample_text):  # noqa: ANN001, ANN201  # tracked: #288
    rows = attr.parse_annotate(sample_text)
    a = attr.aggregate(rows)
    b = attr.aggregate(rows)
    assert [(c["component"], c["ir"]) for c in a] == [(c["component"], c["ir"]) for c in b]
