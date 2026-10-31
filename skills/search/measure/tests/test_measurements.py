"""The `measure` skill's recorder: what it accepts, what it writes, and what it concludes.

The script is a standalone program, so its public surface is its command line (`main`) plus
the pure comparison it exposes (`compare_samples`). The statistics properties are VibeSys's own
(see `libs/vs-core/tests/test_comparison_statistic.py`); the cross-check at the end runs the
same samples through `vs_core` and requires the same verdict, ratio and noise, which is how the
script stays a faithful port without importing it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

if TYPE_CHECKING:
    import random
    from collections.abc import Callable
    from types import ModuleType

    type NewRun = Callable[[], Run]

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measurements.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measurements_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their annotations through this
    spec.loader.exec_module(module)
    return module


script = _load_script()

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
VALUES = st.floats(min_value=1.0, max_value=1e4, allow_nan=False, allow_infinity=False)
DIRECTIONS = st.sampled_from(("max", "min"))


class Run:
    """A repository with one task and one run directory, driven through the command line."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.task = root / ".vibesys" / "tasks" / "demo"
        self.task.mkdir(parents=True)
        (self.task / "OBJECTIVE.md").write_text("Maximize throughput.\n")
        self.dir = root / ".vibesys-optimize" / "run-1"
        self.dir.mkdir(parents=True)
        (self.dir / "run.json").write_text(json.dumps({"task": "demo"}))

    @property
    def lines(self) -> list[str]:
        path = self.dir / "measurements.jsonl"
        return path.read_text().splitlines() if path.exists() else []

    def cli(self, *argv: str, host: str = "host-a") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        code = script.main(list(argv), now=lambda: STAMP, out=out, err=err, host=host)
        return code, out.getvalue(), err.getvalue()

    def record(
        self, commit: str, samples: list[float], **overrides: str | None
    ) -> tuple[int, str, str]:
        """Record samples of a commit; ``overrides`` replace role, direction, metric, check,
        exclusive and host (a ``None`` leaves that optional flag out)."""
        fields: dict[str, str | None] = {
            "role": "candidate",
            "direction": "max",
            "metric": "ops",
            "check": "pass",
            "exclusive": "yes",
            "host": "host-a",
        }
        assert set(overrides) <= set(fields), overrides
        fields.update(overrides)
        argv = [
            "record", "--run", str(self.dir), "--commit", commit, "--role", fields["role"],
            "--label", "run", "--metric", fields["metric"], "--direction", fields["direction"],
            "--command", "bench",
        ]  # fmt: skip
        for value in samples:
            argv += ["--sample", repr(value)]
        for flag in ("check", "exclusive"):
            if fields[flag]:
                argv += [f"--{flag}", fields[flag]]
        return self.cli(*argv, host=fields["host"])

    def compare(self, commit: str, *extra: str) -> dict:
        code, out, err = self.cli(
            "compare", "--run", str(self.dir), "--commit", commit, "--json", *extra
        )
        assert code == 0, err
        return json.loads(out)

    def best(self) -> dict:
        code, out, err = self.cli("best", "--run", str(self.dir), "--json")
        assert code == 0, err
        return json.loads(out)

    def show(self, *extra: str) -> list[dict]:
        code, out, err = self.cli("show", "--run", str(self.dir), "--json", *extra)
        assert code == 0, err
        return json.loads(out)


@pytest.fixture(scope="session")
def new_run(tmp_path_factory: pytest.TempPathFactory) -> NewRun:
    """A factory of fresh runs; per example, so a property test starts from an empty file."""

    def make() -> Run:
        return Run(tmp_path_factory.mktemp("repo"))

    return make


# --- the statistic -----------------------------------------------------------------------------

SAMPLES = st.lists(VALUES, min_size=1, max_size=8)


@given(mine=SAMPLES, theirs=SAMPLES, direction=DIRECTIONS)
def test_fewer_than_three_samples_a_side_never_give_a_verdict(
    mine: list[float], theirs: list[float], direction: str
) -> None:
    if min(len(mine), len(theirs)) < 3:
        assert script.compare_samples(mine, theirs, direction).verdict == "inconclusive"


@given(value=VALUES, other=VALUES, m=st.integers(3, 8), n=st.integers(3, 8), direction=DIRECTIONS)
@example(value=101.0, other=100.0, m=3, n=3, direction="max")
def test_samples_that_are_all_alike_show_no_noise_and_give_no_verdict(
    value: float, other: float, m: int, n: int, direction: str
) -> None:
    result = script.compare_samples([value] * m, [other] * n, direction)
    assert result.verdict == "inconclusive"
    assert result.reason


@given(mine=SAMPLES, theirs=SAMPLES)
def test_which_way_is_better_exchanges_better_and_worse_and_nothing_else(
    mine: list[float], theirs: list[float]
) -> None:
    high = script.compare_samples(mine, theirs, "max")
    low = script.compare_samples(mine, theirs, "min")
    exchanged = {
        "better": "worse",
        "worse": "better",
        "within_noise": "within_noise",
        "inconclusive": "inconclusive",
    }
    assert low.verdict == exchanged[high.verdict]
    assert (high.raw_ratio, high.noise) == (low.raw_ratio, low.noise)


@given(mine=SAMPLES, theirs=SAMPLES, direction=DIRECTIONS)
def test_swapping_the_sides_never_leaves_both_orders_winning_or_both_losing(
    mine: list[float], theirs: list[float], direction: str
) -> None:
    """The bar is a multiple of the noise of the candidate's ratio, so the rule is not exactly
    antisymmetric. What holds: both orders are inconclusive together; they never both win or
    both lose; and a verdict that names the side with the larger mean (a win for "max", a loss
    for "min") becomes the opposite verdict when the sides swap."""
    forward = script.compare_samples(mine, theirs, direction)
    backward = script.compare_samples(theirs, mine, direction)
    assert (forward.verdict == "inconclusive") == (backward.verdict == "inconclusive")
    assert (forward.verdict, backward.verdict) != ("better", "better")
    assert (forward.verdict, backward.verdict) != ("worse", "worse")
    larger_side, other_side = ("better", "worse") if direction == "max" else ("worse", "better")
    if forward.verdict == larger_side:
        assert backward.verdict == other_side


@given(mine=SAMPLES, theirs=SAMPLES, direction=DIRECTIONS, power=st.integers(-20, 20))
def test_a_change_of_unit_changes_no_verdict_ratio_or_noise(
    mine: list[float], theirs: list[float], direction: str, power: int
) -> None:
    scale = 2.0**power  # a power of two scales every float exactly
    a = script.compare_samples(mine, theirs, direction)
    b = script.compare_samples([v * scale for v in mine], [v * scale for v in theirs], direction)
    assert a.verdict == b.verdict
    assert math.isclose(a.ratio, b.ratio, rel_tol=1e-12)
    assert math.isclose(a.noise, b.noise, rel_tol=1e-9, abs_tol=1e-15)


@given(mine=SAMPLES, theirs=SAMPLES, direction=DIRECTIONS, seed=st.randoms(use_true_random=False))
def test_the_order_the_samples_were_taken_in_changes_nothing(
    mine: list[float], theirs: list[float], direction: str, seed: random.Random
) -> None:
    shuffled_mine, shuffled_theirs = list(mine), list(theirs)
    seed.shuffle(shuffled_mine)
    seed.shuffle(shuffled_theirs)
    a = script.compare_samples(mine, theirs, direction)
    b = script.compare_samples(shuffled_mine, shuffled_theirs, direction)
    assert a.verdict == b.verdict
    assert math.isclose(a.ratio, b.ratio, rel_tol=1e-9)


@given(mine=SAMPLES, theirs=SAMPLES)
def test_the_oriented_ratio_is_above_one_exactly_when_the_candidate_is_ahead(
    mine: list[float], theirs: list[float]
) -> None:
    high = script.compare_samples(mine, theirs, "max")
    low = script.compare_samples(mine, theirs, "min")
    assert (high.ratio > 1) == (high.candidate_mean > high.base_mean)
    assert (low.ratio > 1) == (low.candidate_mean < low.base_mean)


def _core_sample(value: float, *, base: bool, direction: str) -> core.Sample:
    anchor = core.BaseRef(
        commit=core.RevisionRef.of_git_commit("base"), measured_input="base-input"
    )
    return core.Sample(
        run=core.RequestId(root=f"run-{base}-{value!r}"),
        scope=core.Scope(owner=core.AttemptId(root="attempt"), generation=0),
        commit=core.RevisionRef.of_git_commit("candidate"),
        measured_input="base-input" if base else "x",
        evaluation="evaluation",
        gates=("accuracy", "benchmark"),
        host="host",
        dedicated_resources=True,
        reading=core.Reading(metric="rate", value=value, direction=direction),
        observed_at=0.0,
        base=None if base else anchor,
    )


def _vs_core_comparison(mine: list[float], theirs: list[float], direction: str) -> core.Comparison:
    result = core.compare(
        tuple(_core_sample(v, base=False, direction=direction) for v in mine),
        tuple(_core_sample(v, base=True, direction=direction) for v in theirs),
        scope=core.Scope(owner=core.AttemptId(root="attempt"), generation=0),
        anchor=core.BaseRef(
            commit=core.RevisionRef.of_git_commit("base"), measured_input="base-input"
        ),
        policy=core.ComparisonPolicy(),
    )
    assert result is not None
    return result


# Up to 40 a side reaches past the 30 degrees of freedom where the quantile table ends.
LONG = st.lists(VALUES, min_size=1, max_size=40)


@given(mine=LONG, theirs=LONG, direction=DIRECTIONS)
@example(
    mine=[110.0, 111.0, 109.0, 110.0, 110.5],
    theirs=[100.0, 101.0, 99.0, 100.0, 100.5],
    direction="max",
)
@example(
    mine=[100.0, 103.0, 97.0, 101.0, 99.0],
    theirs=[100.0, 98.0, 102.0, 101.0, 99.0],
    direction="max",
)
@example(mine=[100.0, 104.0], theirs=[90.0, 91.0, 92.0], direction="max")
@example(mine=[50.0, 51.0, 50.5, 50.2], theirs=[100.0, 101.0, 100.5, 99.9], direction="min")
def test_the_verdict_ratio_and_noise_are_the_ones_vs_core_gives_the_same_samples(
    mine: list[float], theirs: list[float], direction: str
) -> None:
    ours = script.compare_samples(mine, theirs, direction)
    theirs_core = _vs_core_comparison(mine, theirs, direction)
    assert ours.verdict == theirs_core.verdict.value
    assert math.isclose(ours.raw_ratio, theirs_core.ratio, rel_tol=1e-9)
    assert math.isclose(ours.noise, theirs_core.noise, rel_tol=1e-9, abs_tol=1e-15)
    assert (ours.candidate_samples, ours.base_samples) == (
        theirs_core.candidate_samples,
        theirs_core.base_samples,
    )


SWEEP_SHAPES = [(m, n) for m in (3, 4, 5, 8, 12, 20, 40) for n in (3, 5, 9, 20, 40)]


@pytest.mark.parametrize(("m", "n"), SWEEP_SHAPES)
def test_each_critical_value_flips_the_verdict_exactly_where_vs_core_flips_it(
    m: int, n: int
) -> None:
    """Find by bisection the candidate mean at which `vs_core` turns a result into `better`,
    then require the same flip at that mean. The shapes give degrees of freedom from 2 to past
    30, so every row of the Student-t table is the bar for some shape."""
    base = [100.0 + (i % 3 - 1) * 2.0 + (i % 5) * 0.3 for i in range(n)]
    shape = [(i % 4 - 1.5) * 1.5 for i in range(m)]

    def mine(shift: float) -> list[float]:
        return [100.0 + shift + s for s in shape]

    def core_better(shift: float) -> bool:
        return _vs_core_comparison(mine(shift), base, "max").verdict.value == "better"

    low, high = 0.0, 1000.0
    assert not core_better(low)
    assert core_better(high)
    for _ in range(60):
        middle = (low + high) / 2
        low, high = (low, middle) if core_better(middle) else (middle, high)
    assert script.compare_samples(mine(low), base, "max").verdict != "better"
    assert script.compare_samples(mine(high), base, "max").verdict == "better"


def test_golden_cases_have_the_verdicts_the_rule_gives() -> None:
    clear = script.compare_samples([110, 111, 109, 110, 110.5], [100, 101, 99, 100, 100.5], "max")
    assert clear.verdict == "better"
    assert (
        script.compare_samples(
            [110, 111, 109, 110, 110.5], [100, 101, 99, 100, 100.5], "min"
        ).verdict
        == "worse"
    )
    wide = script.compare_samples([100, 120, 80, 105, 95], [100, 98, 102, 101, 99], "max")
    assert wide.verdict == "within_noise"
    two = script.compare_samples([200, 201], [100, 101, 99], "max")
    assert two.verdict == "inconclusive"
    assert "at least 3 samples" in two.reason


def test_a_base_that_averages_zero_has_no_ratio() -> None:
    result = script.compare_samples([1.0, 2.0, 3.0], [-1.0, 0.0, 1.0], "max")
    assert result.verdict == "inconclusive"
    assert result.ratio is None


# --- recording ---------------------------------------------------------------------------------


def test_a_record_carries_what_was_measured_where_and_against_which_task(new_run: NewRun) -> None:
    run = new_run()
    code, out, err = run.record(SHA_A, [1.5, 2.5, 3.5], role="base", host="host-z")
    assert (code, err) == (0, "")
    assert "recorded base" in out
    (record,) = run.show()
    assert record == {
        "schema": 1,
        "recorded_at": "2026-01-02T03:04:05+00:00",
        "host": "host-z",
        "commit": SHA_A,
        "role": "base",
        "label": "run",
        "metric": "ops",
        "direction": "max",
        "command": "bench",
        "samples": [1.5, 2.5, 3.5],
        "check": "pass",
        "exclusive": "yes",
        "evaluator": script.task_fingerprint(run.task),
        "gate_requirements": [],
        "gates": {},
    }


@given(
    runs=st.lists(st.lists(VALUES, min_size=1, max_size=6), min_size=1, max_size=4),
)
def test_samples_come_back_exactly_as_recorded_and_the_file_only_grows(
    new_run: NewRun,
    runs: list[list[float]],
) -> None:
    run = new_run()
    seen: list[str] = []
    for samples in runs:
        assert run.record(SHA_B, samples)[0] == 0
        assert run.lines[: len(seen)] == seen
        seen = run.lines
    assert [r["samples"] for r in run.show()] == runs


@given(commit=st.text(max_size=70))
@example(commit="a" * 40 + "-dirty")
@example(commit="a" * 40 + "+dirty")
@example(commit="A" * 40)
@example(commit="abc1234")
@example(commit="HEAD")
@example(commit="")
def test_anything_but_a_full_lowercase_sha_is_refused_and_nothing_is_written(
    new_run: NewRun,
    commit: str,
) -> None:
    run = new_run()
    code, out, err = run.record(commit, [1.0])
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        assert code == 0
        return
    assert (code, out) == (2, "")
    assert "commit" in err
    assert run.lines == []


def test_a_dirty_tree_marker_is_named_in_the_refusal(new_run: NewRun) -> None:
    run = new_run()
    code, _, err = run.record(SHA_A + "-dirty", [1.0])
    assert code == 2
    assert "dirty" in err


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "NaN", "abc", ""])
def test_a_sample_that_is_not_a_finite_number_is_refused(new_run: NewRun, bad: str) -> None:
    run = new_run()
    code, _, err = run.cli(
        "record", "--run", str(run.dir), "--commit", SHA_A, "--role", "base", "--label", "x",
        "--metric", "ops", "--direction", "max", "--command", "c", "--sample", "1", "--sample", bad,
    )  # fmt: skip
    assert code == 2
    assert "sample" in err or "number" in err or "finite" in err
    assert run.lines == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--surprise", "1"],
        ["--role", "winner"],
        ["--direction", "up"],
        ["--check", "maybe"],
        ["--exclusive", "sometimes"],
    ],
)
def test_an_unknown_flag_or_value_is_refused(new_run: NewRun, extra: list[str]) -> None:
    run = new_run()
    argv = [
        "record", "--run", str(run.dir), "--commit", SHA_A, "--role", "base", "--label", "x",
        "--metric", "ops", "--direction", "max", "--command", "c", "--sample", "1", *extra,
    ]  # fmt: skip
    code, _, err = run.cli(*argv)
    assert code == 2
    assert err.startswith("error:")
    assert run.lines == []


def test_a_directory_that_is_not_a_run_is_refused(tmp_path: Path) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = script.main(
        ["show", "--run", str(tmp_path / "missing")], now=lambda: STAMP, out=out, err=err, host="h"
    )
    assert code == 2
    (tmp_path / "bare").mkdir()
    out, err = io.StringIO(), io.StringIO()
    code = script.main(
        ["record", "--run", str(tmp_path / "bare"), "--commit", SHA_A, "--role", "base",
         "--label", "x", "--metric", "m", "--direction", "max", "--command", "c", "--sample", "1"],
        now=lambda: STAMP, out=out, err=err, host="h",
    )  # fmt: skip
    assert code == 2
    assert "run.json" in err.getvalue()


def test_one_run_has_one_base_and_a_base_is_not_a_candidate(new_run: NewRun) -> None:
    run = new_run()
    assert run.record(SHA_A, [1.0], role="base")[0] == 0
    assert run.record(SHA_A, [2.0], role="base")[0] == 0
    code, _, err = run.record(SHA_B, [1.0], role="base")
    assert code == 2
    assert "base" in err
    code, _, err = run.record(SHA_A, [1.0], role="candidate")
    assert code == 2
    assert "--role base" in err
    assert len(run.lines) == 2


def test_a_metric_keeps_one_direction(new_run: NewRun) -> None:
    run = new_run()
    assert run.record(SHA_A, [1.0], role="base", direction="max")[0] == 0
    code, _, err = run.record(SHA_B, [1.0], direction="min")
    assert code == 2
    assert "direction" in err


def test_a_damaged_file_is_named_by_line_and_stops_every_reader(new_run: NewRun) -> None:
    run = new_run()
    run.record(SHA_A, [1.0], role="base")
    with (run.dir / "measurements.jsonl").open("a") as handle:
        handle.write('{"schema": 1, "extra": true}\n')
    for argv in (["show"], ["best"], ["compare", "--commit", SHA_B]):
        code, _, err = run.cli(*argv[:1], "--run", str(run.dir), *argv[1:])
        assert code == 2
        assert "line 2" in err


def test_the_task_fingerprint_follows_the_task_files_and_nothing_else(tmp_path: Path) -> None:
    task = tmp_path / "task"
    (task / "benchmark").mkdir(parents=True)
    (task / "benchmark" / "b.py").write_text("print(1)\n")
    first = script.task_fingerprint(task)
    assert script.task_fingerprint(task) == first
    (task / "benchmark" / "__pycache__").mkdir()
    (task / "benchmark" / "__pycache__" / "b.pyc").write_bytes(b"\0")
    assert script.task_fingerprint(task) == first
    (task / "benchmark" / "b.py").write_text("print(2)\n")
    assert script.task_fingerprint(task) != first
    (task / "benchmark" / "b.py").write_text("print(1)\n")
    assert script.task_fingerprint(task) == first
    (task / "benchmark" / "b.py").rename(task / "benchmark" / "c.py")
    assert script.task_fingerprint(task) != first


# --- comparing and choosing --------------------------------------------------------------------

BASE = [100.0, 101.0, 99.0, 100.0, 100.5]
FAST = [110.0, 111.0, 109.0, 110.0, 110.5]


def _seeded(run: Run) -> None:
    assert run.record(SHA_A, BASE, role="base")[0] == 0


def test_compare_gives_the_verdict_of_the_samples_recorded(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST)
    result = run.compare(SHA_B)
    expected = script.compare_samples(FAST, BASE, "max")
    assert result["verdict"] == expected.verdict == "better"
    assert result["ratio"] == expected.ratio
    assert (result["candidate_samples"], result["base_samples"]) == (5, 5)
    assert (result["commit"], result["base"], result["check"]) == (SHA_B, SHA_A, "pass")
    assert result["warnings"] == []


def test_samples_of_one_commit_pool_across_its_records(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST[:2])
    assert run.compare(SHA_B)["verdict"] == "inconclusive"
    run.record(SHA_B, FAST[2:])
    pooled = run.compare(SHA_B)
    assert pooled["candidate_samples"] == 5
    assert pooled["verdict"] == "better"


def test_for_a_metric_to_minimize_a_lower_mean_is_a_win(new_run: NewRun) -> None:
    run = new_run()
    assert run.record(SHA_A, FAST, role="base", direction="min", metric="ms")[0] == 0
    run.record(SHA_B, BASE, direction="min", metric="ms")
    result = run.compare(SHA_B)
    assert result["verdict"] == "better"
    assert result["ratio"] > 1


def test_samples_of_another_metric_or_task_version_are_not_compared(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST, metric="other")
    other_metric = run.compare(SHA_B)
    assert other_metric["verdict"] == "inconclusive"
    assert "another metric or evaluator" in other_metric["reason"]
    run.record(SHA_C, FAST)
    assert run.compare(SHA_C)["verdict"] == "better"
    (run.task / "OBJECTIVE.md").write_text("A different objective.\n")
    run.record("d" * 40, FAST)
    changed = run.compare("d" * 40)
    assert changed["verdict"] == "inconclusive"
    assert changed["candidate_samples"] == 0


def test_compare_warns_about_hosts_and_shared_machines(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST, host="host-b", exclusive="no")
    warnings = run.compare(SHA_B)["warnings"]
    assert any("different hosts" in w for w in warnings)
    assert any("shared" in w for w in warnings)


def test_compare_names_what_is_missing(new_run: NewRun) -> None:
    run = new_run()
    code, _, err = run.cli("compare", "--run", str(run.dir), "--commit", SHA_B)
    assert code == 2
    assert "base" in err
    _seeded(run)
    code, _, err = run.cli("compare", "--run", str(run.dir), "--commit", SHA_B)
    assert code == 2
    assert SHA_B in err
    code, _, err = run.cli("compare", "--run", str(run.dir), "--commit", "HEAD")
    assert code == 2


def test_compare_prints_a_line_a_person_can_read(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST)
    code, out, _ = run.cli("compare", "--run", str(run.dir), "--commit", SHA_B)
    assert code == 0
    assert "better" in out
    assert "ratio" in out
    assert SHA_B[:12] in out


def test_a_failed_check_is_reported_and_keeps_a_commit_from_being_best(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST, check="fail")
    assert run.compare(SHA_B)["check"] == "fail"
    assert run.best() == {"best": None}
    run.record(SHA_C, FAST, check=None)
    assert run.compare(SHA_C)["check"] == "unknown"
    assert run.best() == {"best": None}


def test_best_is_the_verdict_backed_commit_with_the_largest_gain(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST)
    run.record(SHA_C, [v * 1.5 for v in FAST])
    run.record("d" * 40, [100.0, 130.0, 70.0, 105.0, 95.0])
    best = run.best()
    assert best["best"]["commit"] == SHA_C
    assert {r["commit"] for r in best["records"]} == {SHA_C}


def test_best_with_no_win_says_so_and_still_exits_cleanly(new_run: NewRun) -> None:
    run = new_run()
    assert run.best() == {"best": None}
    _seeded(run)
    run.record(SHA_B, [100.0, 130.0, 70.0, 105.0, 95.0])
    code, out, _ = run.cli("best", "--run", str(run.dir))
    assert code == 0
    assert "no commit" in out


@given(
    mine=st.lists(VALUES, min_size=1, max_size=6),
    theirs=st.lists(VALUES, min_size=1, max_size=6),
    direction=DIRECTIONS,
)
def test_a_recorded_comparison_equals_the_comparison_of_its_samples(
    new_run: NewRun,
    mine: list[float],
    theirs: list[float],
    direction: str,
) -> None:
    run = new_run()
    assert run.record(SHA_A, theirs, role="base", direction=direction)[0] == 0
    assert run.record(SHA_B, mine, direction=direction)[0] == 0
    direct = script.compare_samples(mine, theirs, direction)
    recorded = run.compare(SHA_B)
    assert recorded["verdict"] == direct.verdict
    assert recorded["ratio"] == direct.ratio
    assert recorded["noise"] == direct.noise


def test_show_filters_by_commit(new_run: NewRun) -> None:
    run = new_run()
    _seeded(run)
    run.record(SHA_B, FAST)
    assert [r["commit"] for r in run.show("--commit", SHA_B)] == [SHA_B]
    code, out, _ = run.cli("show", "--run", str(run.dir))
    assert code == 0
    assert out.count("\n") == 2
    assert run.cli("show", "--run", str(run.dir), "--commit", SHA_C)[1] == "no records\n"


@pytest.mark.parametrize("stamp", ["not-a-date", "2026-01-01T00:00:00", "2026-99-99T00:00:00Z"])
def test_record_timestamp_requires_a_real_zoned_instant(new_run: NewRun, stamp: str) -> None:
    run = new_run()
    assert run.record(SHA_A, [1, 2, 3], role="base")[0] == 0
    (record,) = run.show()
    record["recorded_at"] = stamp
    with pytest.raises(script.InvalidInputError, match="recorded_at"):
        script.validate_record(record)


def test_task_hash_ignores_generated_results_and_environment(new_run: NewRun) -> None:
    run = new_run()
    before = script.task_fingerprint(run.task)
    for name in ("results.json", ".env", ".env.local", "agent.toml", "__pycache__/generated.pyc"):
        path = run.task / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated output")
    assert script.task_fingerprint(run.task) == before
    (run.task / "OBJECTIVE.md").write_text("changed objective")
    assert script.task_fingerprint(run.task) != before
