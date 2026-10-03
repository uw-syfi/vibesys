"""Tests for resources/profilers/rocprof/kernel_bench.py.

That module is a standalone, stdlib-only CLI + library (see its module
docstring): no vibesys imports, staged verbatim into the agent workspace as
``rocprof_profiler/kernel_bench.py``. It is loaded here by file path, the
same way ``tests/vibesys/loops/test_profiler_mcp.py`` loads the sibling
nsys/torch profiler modules, so these tests stay decoupled from
sys.path/package state.

``time_callable`` and ``paired_ab`` import torch lazily and are not
exercised here (this environment has no torch/GPU). Instead these tests
drive the same decision logic those two functions call into --
``summarize_series`` and ``assess_paired``/``assess_unpaired`` -- directly
with fake, already-recorded ``wall_ms`` series, exactly the "record timings
from any driver" path the module's CLI is built for.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import statistics
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

_MODULE_NAME = "rocprof_kernel_bench_under_test"
_MODULE_PATH = (
    Path(__file__).resolve().parents[3] / "resources" / "profilers" / "rocprof" / "kernel_bench.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # Dataclasses with `from __future__ import annotations` resolve their
    # field types via `sys.modules[cls.__module__]`, so the module must be
    # registered under its own name before `exec_module` runs.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        del sys.modules[name]
        raise
    return module


# Loaded once at collection time (not inside a fixture) so the `from
# rocprof_kernel_bench_under_test import _private_helper` statements below
# can resolve it through sys.modules -- plain name references instead of a
# private-attribute access pattern.
_load_module(_MODULE_NAME, _MODULE_PATH)

from rocprof_kernel_bench_under_test import (  # noqa: E402  (module must load first)  # ty: ignore[unresolved-import]  # LW-900101; the module-under-test is loaded by file path above, so the static import target does not exist on disk
    DEFAULT_THRESHOLD_PCT,
    MIN_PAIRS_FOR_VERDICT,
    MIN_SAMPLES_FOR_TREND,
    MIN_SAMPLES_FOR_VERDICT,
    _is_monotonic_increasing,
    _is_valid_pair,
    _load_pairs,
    _load_wall_ms,
    _percentile,
    _print_verdict,
    assess_paired,
    assess_unpaired,
    summarize_series,
)


@pytest.fixture(scope="module")
def kb() -> ModuleType:
    return sys.modules[_MODULE_NAME]


def test_module_imports_without_torch():  # noqa: ANN201  # LW-920340; tracked migration debt from the pre-manifest ratchet scheme
    """The module never imports torch at import time (only inside time_callable/paired_ab)."""
    assert "torch" not in sys.modules


# ---------------------------------------------------------------------------
# _percentile / _is_monotonic_increasing
# ---------------------------------------------------------------------------


def test_percentile_of_empty_sequence_is_zero():  # noqa: ANN201  # LW-920341; tracked migration debt from the pre-manifest ratchet scheme
    assert _percentile([], 50) == 0.0


def test_percentile_of_single_value_is_that_value():  # noqa: ANN201  # LW-920342; tracked migration debt from the pre-manifest ratchet scheme
    assert _percentile([7.0], 90) == 7.0


def test_percentile_interpolates_linearly():  # noqa: ANN201  # LW-920343; tracked migration debt from the pre-manifest ratchet scheme
    values = [float(v) for v in range(1, 11)]  # 1..10, already sorted

    assert _percentile(values, 10) == pytest.approx(1.9)
    assert _percentile(values, 90) == pytest.approx(9.1)
    assert _percentile(values, 50) == pytest.approx(5.5)


def test_is_monotonic_increasing_requires_at_least_three_points():  # noqa: ANN201  # LW-920344; tracked migration debt from the pre-manifest ratchet scheme
    assert _is_monotonic_increasing([1.0, 2.0]) is False
    assert _is_monotonic_increasing([1.0, 2.0, 3.0]) is True


def test_is_monotonic_increasing_false_on_any_non_rise():  # noqa: ANN201  # LW-920345; tracked migration debt from the pre-manifest ratchet scheme
    assert _is_monotonic_increasing([1.0, 2.0, 2.0, 3.0]) is False
    assert _is_monotonic_increasing([3.0, 2.0, 1.0]) is False


# ---------------------------------------------------------------------------
# summarize_series
# ---------------------------------------------------------------------------


def test_summarize_series_drops_warmup(kb):  # noqa: ANN001, ANN201  # LW-920346; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    result = kb.summarize_series([100.0, 50.0, 10.0, 10.0, 10.0], warmup=2)

    assert result.warmup_ms == (100.0, 50.0)
    assert result.samples_ms == (10.0, 10.0, 10.0)


def test_summarize_series_insufficient_samples(kb):  # noqa: ANN001, ANN201  # LW-920347; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    result = kb.summarize_series([10.0], warmup=0)

    assert not result.converged
    assert result.reason == "insufficient_samples"
    assert result.median_ms is None
    assert result.p10_ms is None
    assert result.p90_ms is None
    assert result.spread_pct is None


def test_summarize_series_flags_monotonic_increasing_as_not_converged(kb):  # noqa: ANN001, ANN201  # LW-920348; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    result = kb.summarize_series([1.0, 2.0, 3.0, 4.0], warmup=0)

    assert not result.converged
    assert result.reason == "monotonic_increasing"
    # Still reports the stats -- just flagged as unusable.
    assert result.median_ms == pytest.approx(statistics.median([1.0, 2.0, 3.0, 4.0]))


def test_summarize_series_converged_reports_correct_stats(kb):  # noqa: ANN001, ANN201  # LW-920349; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    samples = [10.0, 12.0, 9.0, 11.0, 10.5]

    result = kb.summarize_series(samples, warmup=0)

    assert result.converged
    assert result.reason == "converged"
    assert result.median_ms == pytest.approx(statistics.median(samples))
    ordered = sorted(samples)
    assert result.p10_ms == pytest.approx(_percentile(ordered, 10))
    assert result.p90_ms == pytest.approx(_percentile(ordered, 90))
    expected_spread = (result.p90_ms - result.p10_ms) / result.median_ms * 100.0
    assert result.spread_pct == pytest.approx(expected_spread)


def test_timing_result_to_dict_round_trips_fields(kb):  # noqa: ANN001, ANN201  # LW-920350; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    result = kb.summarize_series([10.0, 11.0, 9.0], warmup=0)

    payload = result.to_dict()

    assert payload["converged"] is True
    assert payload["reason"] == "converged"
    assert payload["samples_ms"] == [10.0, 11.0, 9.0]
    assert json.dumps(payload)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# assess_paired
# ---------------------------------------------------------------------------


def test_assess_paired_not_converged_when_too_few_pairs(kb):  # noqa: ANN001, ANN201  # LW-920351; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    verdict = kb.assess_paired([(10.0, 9.0)])

    assert not verdict.decisive
    assert verdict.reason == "not_converged"
    assert verdict.median_delta_pct is None


def test_assess_paired_not_converged_when_still_climbing(kb):  # noqa: ANN001, ANN201  # LW-920352; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # Candidate side is monotonically increasing across pairs -- not settled yet.
    pairs = [(10.0, 8.0), (10.0, 9.0), (10.0, 10.0), (10.0, 11.0)]

    verdict = kb.assess_paired(pairs)

    assert not verdict.decisive
    assert verdict.reason == "not_converged"


def test_assess_paired_sign_disagreement(kb):  # noqa: ANN001, ANN201  # LW-920353; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # Candidate wins some pairs, loses others -- no consistent verdict.
    pairs = [(10.0, 8.0), (10.0, 12.0), (10.0, 8.5), (10.0, 11.5)]

    verdict = kb.assess_paired(pairs)

    assert not verdict.decisive
    assert verdict.reason == "sign_disagreement"
    assert not verdict.candidate_faster


def test_assess_paired_within_noise(kb):  # noqa: ANN001, ANN201  # LW-920354; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # `assess_paired` requires every pair's delta to agree in sign (see
    # `sign_disagreement` below), so "within noise" here means consistently
    # signed but small, not straddling zero.
    pairs = [(10.00, 10.05), (10.00, 10.02), (10.00, 10.08), (10.00, 10.01)]

    verdict = kb.assess_paired(pairs, threshold_pct=3.0)

    assert verdict.decisive
    assert verdict.reason == "within_noise"
    assert abs(verdict.median_delta_pct) <= 3.0


def test_assess_paired_candidate_faster(kb):  # noqa: ANN001, ANN201  # LW-920355; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    pairs = [(10.0, 8.0), (10.0, 8.1), (10.0, 7.9), (10.0, 8.0)]

    verdict = kb.assess_paired(pairs, threshold_pct=3.0)

    assert verdict.decisive
    assert verdict.reason == "faster"
    assert verdict.candidate_faster
    assert verdict.median_delta_pct < 0


def test_assess_paired_candidate_slower(kb):  # noqa: ANN001, ANN201  # LW-920356; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    pairs = [(10.0, 12.0), (10.0, 12.1), (10.0, 11.9), (10.0, 12.0)]

    verdict = kb.assess_paired(pairs, threshold_pct=3.0)

    assert verdict.decisive
    assert verdict.reason == "slower"
    assert not verdict.candidate_faster
    assert verdict.median_delta_pct > 0


def test_assess_paired_drops_non_positive_samples(kb):  # noqa: ANN001, ANN201  # LW-920357; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    pairs = [(10.0, 8.0), (0.0, 8.0), (-1.0, 8.0), (10.0, 8.1)]

    verdict = kb.assess_paired(pairs, threshold_pct=3.0)

    # Only the two well-formed pairs are usable.
    assert len(verdict.pairs) == 2
    assert verdict.decisive
    assert verdict.reason == "faster"


def test_paired_verdict_to_dict_round_trips(kb):  # noqa: ANN001, ANN201  # LW-920358; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    verdict = kb.assess_paired([(10.0, 8.0), (10.0, 8.1)], threshold_pct=3.0)

    payload = verdict.to_dict()

    assert payload["decisive"] is True
    assert payload["reason"] == "faster"
    assert payload["candidate_faster"] is True
    assert json.dumps(payload)


# ---------------------------------------------------------------------------
# assess_unpaired
# ---------------------------------------------------------------------------


def test_assess_unpaired_not_converged_when_either_side_lacks_samples(kb):  # noqa: ANN001, ANN201  # LW-920359; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    verdict = kb.assess_unpaired([10.0], [10.0, 9.0, 10.5])

    assert not verdict.decisive
    assert verdict.reason == "not_converged"


def test_assess_unpaired_faster_slower_and_within_noise(kb):  # noqa: ANN001, ANN201  # LW-920360; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    baseline = [10.0, 10.1, 9.9, 10.0]

    faster = kb.assess_unpaired(baseline, [8.0, 8.1, 7.9, 8.0], threshold_pct=3.0)
    slower = kb.assess_unpaired(baseline, [12.0, 12.1, 11.9, 12.0], threshold_pct=3.0)
    noisy = kb.assess_unpaired(baseline, [10.05, 9.95, 10.0, 10.02], threshold_pct=3.0)

    assert faster.decisive
    assert faster.reason == "faster"
    assert slower.decisive
    assert slower.reason == "slower"
    assert noisy.decisive
    assert noisy.reason == "within_noise"


# ---------------------------------------------------------------------------
# CLI loading helpers
# ---------------------------------------------------------------------------


def test_is_valid_pair():  # noqa: ANN201  # LW-920361; tracked migration debt from the pre-manifest ratchet scheme
    assert _is_valid_pair([1.0, 2.0]) is True
    assert _is_valid_pair([1, 2]) is True
    assert _is_valid_pair([1.0, 2.0, 3.0]) is False
    assert _is_valid_pair([1.0, "two"]) is False
    assert _is_valid_pair("not-a-list") is False


def test_load_wall_ms_reads_valid_file(tmp_path):  # noqa: ANN001, ANN201  # LW-920362; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"wall_ms": [1.0, 2.5, 3]}), encoding="utf-8")

    assert _load_wall_ms(str(path)) == [1.0, 2.5, 3.0]


def test_load_wall_ms_exits_when_key_missing(tmp_path):  # noqa: ANN001, ANN201  # LW-920363; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")

    with pytest.raises(SystemExit):
        _load_wall_ms(str(path))


def test_load_wall_ms_exits_on_non_numeric_values(tmp_path):  # noqa: ANN001, ANN201  # LW-920364; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"wall_ms": [1.0, "oops"]}), encoding="utf-8")

    with pytest.raises(SystemExit):
        _load_wall_ms(str(path))


def test_load_pairs_reads_valid_file(tmp_path):  # noqa: ANN001, ANN201  # LW-920365; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"pairs": [[10.0, 8.0], [10.0, 8.1]]}), encoding="utf-8")

    assert _load_pairs(str(path)) == [(10.0, 8.0), (10.0, 8.1)]


def test_load_pairs_exits_when_key_missing(tmp_path):  # noqa: ANN001, ANN201  # LW-920366; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"nope": []}), encoding="utf-8")

    with pytest.raises(SystemExit):
        _load_pairs(str(path))


def test_load_pairs_exits_on_malformed_pair(tmp_path):  # noqa: ANN001, ANN201  # LW-920367; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    path = tmp_path / "pairs.json"
    path.write_text(json.dumps({"pairs": [[10.0, 8.0], [10.0]]}), encoding="utf-8")

    with pytest.raises(SystemExit):
        _load_pairs(str(path))


def test_print_verdict_reports_decisive_and_pairs(capsys, kb):  # noqa: ANN001, ANN201  # LW-920368; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    verdict = kb.assess_paired([(10.0, 8.0), (10.0, 8.1)], threshold_pct=3.0)

    _print_verdict(verdict, "baseline", "candidate")

    out = capsys.readouterr().out
    assert "DECISIVE: faster" in out
    assert "baseline -> candidate" in out
    assert "pairs used: 2" in out


def test_print_verdict_reports_inconclusive_without_pairs_line(capsys, kb):  # noqa: ANN001, ANN201  # LW-920369; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    verdict = kb.assess_unpaired([10.0], [10.0, 9.0, 10.0])

    _print_verdict(verdict, "a.json", "b.json")

    out = capsys.readouterr().out
    assert "INCONCLUSIVE: not_converged" in out
    assert "n/a" in out
    assert "pairs used" not in out


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def test_cmd_parse_extracts_wall_ms_lines(tmp_path, capsys, kb):  # noqa: ANN001, ANN201  # LW-920370; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    log = tmp_path / "driver.log"
    log.write_text(
        "starting up\nwall_ms: 12.3\nnoise line\nwall_ms: 11.9\nwall_ms: 1.2e1\n",
        encoding="utf-8",
    )

    kb.cmd_parse(argparse.Namespace(log=str(log)))

    payload = json.loads(capsys.readouterr().out)
    assert payload == {"wall_ms": [12.3, 11.9, 12.0]}


def test_cmd_compare_prints_verdict(tmp_path, capsys, kb):  # noqa: ANN001, ANN201  # LW-920371; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    file_a = tmp_path / "a.json"
    file_b = tmp_path / "b.json"
    file_a.write_text(json.dumps({"wall_ms": [10.0, 10.1, 9.9, 10.0]}), encoding="utf-8")
    file_b.write_text(json.dumps({"wall_ms": [8.0, 8.1, 7.9, 8.0]}), encoding="utf-8")

    kb.cmd_compare(argparse.Namespace(file_a=str(file_a), file_b=str(file_b), threshold_pct=3.0))

    out = capsys.readouterr().out
    assert "DECISIVE: faster" in out


def test_cmd_verdict_prints_verdict(tmp_path, capsys, kb):  # noqa: ANN001, ANN201  # LW-920372; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    samples = tmp_path / "pairs.json"
    samples.write_text(
        json.dumps({"pairs": [[10.0, 8.0], [10.0, 8.1], [10.0, 7.9]]}), encoding="utf-8"
    )

    kb.cmd_verdict(argparse.Namespace(samples=str(samples), threshold_pct=3.0))

    out = capsys.readouterr().out
    assert "DECISIVE: faster" in out
    assert "pairs used: 3" in out


def test_main_dispatches_parse(tmp_path, capsys, kb):  # noqa: ANN001, ANN201  # LW-920373; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    log = tmp_path / "driver.log"
    log.write_text("wall_ms: 5.0\n", encoding="utf-8")

    kb.main(["parse", str(log)])

    assert json.loads(capsys.readouterr().out) == {"wall_ms": [5.0]}


def test_main_dispatches_verdict_with_threshold_flag(tmp_path, capsys, kb):  # noqa: ANN001, ANN201  # LW-920374; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # Consistently signed (both slightly slower), so a tiny threshold makes
    # this a decisive "slower" instead of "within_noise".
    samples = tmp_path / "pairs.json"
    samples.write_text(json.dumps({"pairs": [[10.0, 10.05], [10.0, 10.02]]}), encoding="utf-8")

    kb.main(["verdict", str(samples), "--threshold-pct", "0.001"])

    out = capsys.readouterr().out
    assert "DECISIVE: slower" in out


def test_main_requires_a_subcommand(kb):  # noqa: ANN001, ANN201  # LW-920375; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    with pytest.raises(SystemExit):
        kb.main([])


# ---------------------------------------------------------------------------
# Property tests -- decision logic invariants over the plain-float series
# these functions work on (never CSV/rocprof-specific data).
# ---------------------------------------------------------------------------

_POS = st.floats(min_value=0.01, max_value=1_000.0, allow_nan=False, allow_infinity=False)
_SELF_PAIR_MIN = max(MIN_SAMPLES_FOR_VERDICT, MIN_PAIRS_FOR_VERDICT)


def _cumulative_increasing(increments: list[float]) -> list[float]:
    """A strictly increasing series built from positive increments."""
    total = 1.0
    out = []
    for inc in increments:
        total += inc
        out.append(total)
    return out


_MONOTONIC_SERIES = st.lists(_POS, min_size=MIN_SAMPLES_FOR_TREND, max_size=15).map(
    _cumulative_increasing
)


# 1. Symmetric verdict: swapping A/B flips faster/slower.


@settings(max_examples=25, deadline=None)
@given(
    direction=st.sampled_from((1, -1)),
    magnitude=st.floats(min_value=DEFAULT_THRESHOLD_PCT + 2.0, max_value=90.0),
    a_values=st.lists(_POS, min_size=MIN_PAIRS_FOR_VERDICT, max_size=15),
)
def test_assess_paired_swap_flips_faster_and_slower(direction, magnitude, a_values):  # noqa: ANN001, ANN201  # LW-920376; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # Every pair shares the exact same signed delta -- decisively away from
    # the threshold and never sign-disagreeing -- so the verdict is
    # deterministic by construction instead of relying on `assume()` to
    # reject the (common) indecisive cases from fully independent A/B draws.
    pairs = [(a, a * (1 + direction * magnitude / 100.0)) for a in a_values]
    a_series = [a for a, _ in pairs]
    b_series = [b for _, b in pairs]
    assume(not _is_monotonic_increasing(a_series))
    assume(not _is_monotonic_increasing(b_series))

    forward = assess_paired(pairs)
    assert forward.decisive
    assert forward.reason == ("slower" if direction > 0 else "faster")

    backward = assess_paired([(b, a) for a, b in pairs])

    assert backward.decisive
    assert backward.reason == ("faster" if direction > 0 else "slower")
    assert (forward.median_delta_pct > 0) != (backward.median_delta_pct > 0)


@settings(max_examples=25, deadline=None)
@given(
    direction=st.sampled_from((1, -1)),
    magnitude=st.floats(min_value=DEFAULT_THRESHOLD_PCT + 2.0, max_value=90.0),
    samples_a=st.lists(_POS, min_size=MIN_SAMPLES_FOR_VERDICT, max_size=15),
)
def test_assess_unpaired_swap_flips_faster_and_slower(direction, magnitude, samples_a):  # noqa: ANN001, ANN201  # LW-920377; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    # `assess_unpaired` compares medians, so scaling every A sample by the
    # same factor scales the median by the same factor -- deterministically
    # decisive, same rationale as the paired version above.
    samples_b = [a * (1 + direction * magnitude / 100.0) for a in samples_a]
    assume(not _is_monotonic_increasing(samples_a))
    assume(not _is_monotonic_increasing(samples_b))

    forward = assess_unpaired(samples_a, samples_b)
    assert forward.decisive
    assert forward.reason == ("slower" if direction > 0 else "faster")

    backward = assess_unpaired(samples_b, samples_a)

    assert backward.decisive
    assert backward.reason == ("faster" if direction > 0 else "slower")
    assert (forward.median_delta_pct > 0) != (backward.median_delta_pct > 0)


# 2. within_noise for identical distributions.


@settings(max_examples=25, deadline=None)
@given(samples=st.lists(_POS, min_size=_SELF_PAIR_MIN, max_size=15))
def test_assess_paired_within_noise_when_pairing_each_value_with_itself(samples):  # noqa: ANN001, ANN201  # LW-920378; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    assume(not _is_monotonic_increasing(samples))

    verdict = assess_paired([(x, x) for x in samples])

    assert verdict.decisive
    assert verdict.reason == "within_noise"
    assert verdict.median_delta_pct == 0.0
    assert all(delta == 0.0 for delta in verdict.deltas_pct)


@settings(max_examples=25, deadline=None)
@given(samples=st.lists(_POS, min_size=_SELF_PAIR_MIN, max_size=15))
def test_assess_unpaired_within_noise_for_a_series_against_itself(samples):  # noqa: ANN001, ANN201  # LW-920379; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    assume(not _is_monotonic_increasing(samples))

    verdict = assess_unpaired(samples, samples)

    assert verdict.decisive
    assert verdict.reason == "within_noise"
    assert verdict.median_delta_pct == 0.0


# 3. sign_disagreement when pair deltas' signs are genuinely mixed.


@settings(max_examples=25, deadline=None)
@given(
    threshold_pct=st.floats(min_value=0.1, max_value=30.0, allow_nan=False, allow_infinity=False),
    epsilon=st.floats(min_value=0.01, max_value=0.5, allow_nan=False, allow_infinity=False),
    n_slower=st.integers(min_value=1, max_value=5),
    n_faster=st.integers(min_value=1, max_value=5),
)
def test_assess_paired_sign_disagreement_from_mixed_pair_signs(  # noqa: ANN201  # LW-920380; tracked migration debt from the pre-manifest ratchet scheme
    threshold_pct,  # noqa: ANN001  # LW-920381; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
    epsilon,  # noqa: ANN001  # LW-920382; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
    n_slower,  # noqa: ANN001  # LW-920383; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
    n_faster,  # noqa: ANN001  # LW-920384; this parameter's type is intentionally left loose; annotating it now is separate cleanup work
):
    base = 100.0
    fraction = threshold_pct / 100.0 + epsilon
    assume(fraction < 1.0)  # keep the "faster" side's candidate time positive

    slower_pairs = [(base, base * (1 + fraction))] * n_slower
    faster_pairs = [(base, base * (1 - fraction))] * n_faster
    pairs = slower_pairs + faster_pairs

    verdict = assess_paired(pairs, threshold_pct=threshold_pct)

    assert not verdict.decisive
    assert verdict.reason == "sign_disagreement"


# 4. not_converged for a monotonic series.


@settings(max_examples=20, deadline=None)
@given(series=_MONOTONIC_SERIES)
def test_summarize_series_flags_strictly_increasing_series_as_not_converged(series):  # noqa: ANN001, ANN201  # LW-920385; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    result = summarize_series(series, warmup=0)

    assert result.converged is False
    assert result.reason == "monotonic_increasing"


@settings(max_examples=20, deadline=None)
@given(data=st.data())
def test_assess_paired_not_converged_when_either_side_is_monotonic(data):  # noqa: ANN001, ANN201  # LW-920386; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    monotonic = data.draw(_MONOTONIC_SERIES)
    other = data.draw(st.lists(_POS, min_size=len(monotonic), max_size=len(monotonic)))

    monotonic_is_a = assess_paired(list(zip(monotonic, other, strict=True)))
    monotonic_is_b = assess_paired(list(zip(other, monotonic, strict=True)))

    assert not monotonic_is_a.decisive
    assert monotonic_is_a.reason == "not_converged"
    assert not monotonic_is_b.decisive
    assert monotonic_is_b.reason == "not_converged"


@settings(max_examples=20, deadline=None)
@given(data=st.data())
def test_assess_unpaired_not_converged_when_either_side_is_monotonic(data):  # noqa: ANN001, ANN201  # LW-920387; this parameter's type is intentionally left loose; annotating it now is separate cleanup work; tracked migration debt from the pre-manifest ratchet scheme
    monotonic = data.draw(_MONOTONIC_SERIES)
    other = data.draw(st.lists(_POS, min_size=MIN_SAMPLES_FOR_VERDICT, max_size=15))

    monotonic_is_a = assess_unpaired(monotonic, other)
    monotonic_is_b = assess_unpaired(other, monotonic)

    assert not monotonic_is_a.decisive
    assert monotonic_is_a.reason == "not_converged"
    assert not monotonic_is_b.decisive
    assert monotonic_is_b.reason == "not_converged"
