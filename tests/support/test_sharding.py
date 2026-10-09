"""Properties of the CI test sharding (tests/support/sharding.py)."""

from __future__ import annotations

import json
import os
import tomllib
from collections import Counter
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from tests.support.sharding import (
    DEFAULT_DURATIONS,
    HEAVY_SHARE,
    SHARD_BUDGET_SECONDS,
    assign_heavy_items,
    assign_shards,
    discover_test_files,
    drift_report,
    heavy_threshold,
    is_heavy,
    merge_durations,
    order_test_indices,
    parse_durations,
    parse_shard,
    projected_shard_seconds,
    read_durations,
    record_durations,
    shard_loads,
)

pytest_plugins = ["pytester"]

_REPO = Path(__file__).resolve().parents[2]

_NAMES = st.text(alphabet="abcdefgh/_.", min_size=1, max_size=12)
_DURATIONS = st.dictionaries(_NAMES, st.floats(min_value=0, max_value=500), max_size=30)
_WHOLE_DURATIONS = st.dictionaries(_NAMES, st.floats(min_value=0, max_value=500), max_size=30)
_FILES = st.lists(_NAMES, min_size=1, max_size=40)
_COUNTS = st.integers(min_value=1, max_value=6)


@given(files=_FILES, durations=_DURATIONS)
def test_duration_order_preserves_every_item_and_each_files_order(
    files: list[str], durations: dict[str, float]
) -> None:
    order = order_test_indices((f"{name}::test" for name in files), durations)

    assert sorted(order) == list(range(len(files)))
    for name in set(files):
        assert [index for index in order if files[index] == name] == [
            index for index, file in enumerate(files) if file == name
        ]
    counts = Counter(files)
    known = [durations[name] for name in counts if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = [durations.get(files[index], fallback) / counts[files[index]] for index in order]
    assert weights == sorted(weights, reverse=True)


def test_duration_order_starts_long_tests_before_a_large_fast_file() -> None:
    nodeids = [*(f"fast.py::test_{index}" for index in range(100)), "slow.py::test"]

    assert order_test_indices(nodeids, {"fast.py": 100.0, "slow.py": 50.0}) == [
        100,
        *range(100),
    ]
    assert order_test_indices([], {}) == []


def test_duration_order_estimates_unknown_files_from_known_files() -> None:
    assert order_test_indices(
        ["unknown.py::first", "unknown.py::second", "known.py::test"],
        {"known.py": 100.0},
    ) == [2, 0, 1]
    assert order_test_indices(["large.py::first", "large.py::second", "small.py::test"], {}) == [
        2,
        0,
        1,
    ]


@given(files=_FILES, durations=_WHOLE_DURATIONS, count=_COUNTS)
def test_every_file_lands_in_exactly_one_valid_shard(
    files: list[str], durations: dict[str, float], count: int
) -> None:
    assignment = assign_shards(files, durations, count)

    assert set(assignment) == {name for name in files if not is_heavy(name, durations, count)}
    assert all(1 <= shard <= count for shard in assignment.values())


@given(durations=_WHOLE_DURATIONS, count=_COUNTS, data=st.data())
def test_the_assignment_does_not_depend_on_collection_order(
    durations: dict[str, float], count: int, data: st.DataObject
) -> None:
    files = data.draw(_FILES)
    shuffled = data.draw(st.permutations(files))

    assert assign_shards(shuffled, durations, count) == assign_shards(files, durations, count)


@given(files=_FILES, durations=_WHOLE_DURATIONS, count=_COUNTS)
def test_the_heaviest_shard_exceeds_the_mean_by_less_than_one_file(
    files: list[str], durations: dict[str, float], count: int
) -> None:
    assignment = assign_shards(files, durations, count)
    unique = {name for name in files if not is_heavy(name, durations, count)}
    known = [durations[name] for name in unique if name in durations]
    fallback = sum(known) / len(known) if known else 1.0
    weights = {name: durations.get(name, fallback) for name in unique}
    loads = [0.0] * count
    for name, shard in assignment.items():
        loads[shard - 1] += weights[name]

    assert max(loads, default=0.0) <= (
        sum(weights.values()) / count + max(weights.values(), default=0.0) + 1e-6
    )


@given(count=_COUNTS, data=st.data())
def test_parse_shard_round_trips_every_valid_spec(count: int, data: st.DataObject) -> None:
    index = data.draw(st.integers(min_value=1, max_value=count))

    assert parse_shard(f"{index}/{count}") == (index, count)


@pytest.mark.parametrize("spec", ["", "1", "0/2", "3/2", "a/b", "1/", "/2", "-1/2", "1/2/3"])
def test_parse_shard_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(ValueError, match="--shard"):
        parse_shard(spec)


_ITEM_COUNTS = st.dictionaries(_NAMES, st.integers(min_value=1, max_value=30), max_size=10)


@given(durations=_DURATIONS, counts=_ITEM_COUNTS, count=_COUNTS)
def test_every_test_runs_in_exactly_one_shard_whether_or_not_its_file_is_heavy(
    durations: dict[str, float], counts: dict[str, int], count: int
) -> None:
    whole, loads = shard_loads(counts, durations, count)
    spread, _ = assign_heavy_items(counts, durations, loads)

    for name, total in counts.items():
        owners = (
            [spread[name, index] for index in range(total)]
            if is_heavy(name, durations, count)
            else [whole[name]]
        )
        assert all(1 <= owner <= count for owner in owners)
    assert set(whole) == {name for name in counts if not is_heavy(name, durations, count)}
    assert {name for name, _ in spread} == {
        name for name in counts if is_heavy(name, durations, count)
    }
    assert len(spread) == sum(
        total for name, total in counts.items() if is_heavy(name, durations, count)
    )


def test_a_heavy_file_is_spread_over_every_shard_instead_of_filling_one() -> None:
    durations = {"sweep.py": 1000.0, "small.py": 1.0}
    whole, loads = shard_loads(["sweep.py", "small.py"], durations, 4)
    spread, final = assign_heavy_items({"sweep.py": 40}, durations, loads)

    assert set(whole) == {"small.py"}
    assert set(spread.values()) == {1, 2, 3, 4}
    assert max(final) - min(final) <= durations["sweep.py"] / 40
    assert max(final) < durations["sweep.py"] / 2


@given(
    records=st.lists(
        st.dictionaries(_NAMES, st.floats(min_value=0, max_value=500), max_size=8), max_size=5
    )
)
def test_merging_shard_records_keeps_every_file_and_adds_its_seconds(
    records: list[dict[str, float]],
) -> None:
    merged = merge_durations(records)

    assert set(merged) == {name for record in records for name in record}
    for name, seconds in merged.items():
        assert seconds == pytest.approx(
            sum(r.get(name, 0.0) for r in records), abs=0.01 * len(records)
        )


def test_a_file_missing_from_the_record_or_far_over_it_is_reported_as_stale() -> None:
    seconds, stale = drift_report(
        {"new.py": 600.0, "old.py": 100.0, "close.py": 50.0, "tiny.py": 20.0},
        {"old.py": 90.0, "close.py": 30.0, "tiny.py": 1.0},
    )

    assert seconds == pytest.approx(385.0)
    assert [line.split(":")[0] for line in stale] == ["new.py"]


def test_discovery_lists_test_files_and_skips_caches_and_hidden_directories(tmp_path: Path) -> None:
    for relative in [
        "a/test_x.py",
        "a/y_test.py",
        "a/helper.py",
        "a/__pycache__/test_z.py",
        "a/.hid/test_w.py",
    ]:
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text("")

    assert discover_test_files(tmp_path, ["a", "missing"], ["test_*.py", "*_test.py"]) == [
        "a/test_x.py",
        "a/y_test.py",
    ]


def test_the_checked_in_record_keeps_every_ci_shard_within_its_budget() -> None:
    durations = json.loads(DEFAULT_DURATIONS.read_text())
    workflow = yaml.safe_load((_REPO / ".github/workflows/test.yml").read_text())
    shards = len(workflow["jobs"]["test"]["strategy"]["matrix"]["shard"])
    options = tomllib.loads((_REPO / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]
    files = discover_test_files(_REPO, options["testpaths"], ["test_*.py", "*_test.py"])

    projected = projected_shard_seconds(files, durations, shards)

    assert max(projected) <= SHARD_BUDGET_SECONDS, (
        f"projected slowest shard {max(projected):.0f}s exceeds {SHARD_BUDGET_SECONDS:.0f}s: "
        "add shards in .github/workflows/test.yml or refresh tests/support/shard_durations.json"
    )


@given(first=_DURATIONS, second=_DURATIONS)
def test_a_second_pytest_run_of_a_shard_adds_to_the_record_of_the_first(
    first: dict[str, float], second: dict[str, float], tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("record") / "shard.json"
    path.write_text(json.dumps(record_durations(first, path)))

    record = record_durations(second, path)

    assert set(record) == set(first) | set(second)
    for name in set(first) | set(second):
        expected = second[name] if name in second else first[name]
        assert record[name] == pytest.approx(expected, abs=0.01)


@given(durations=_DURATIONS)
def test_a_written_record_reads_back_unchanged(
    durations: dict[str, float], tmp_path_factory: pytest.TempPathFactory
) -> None:
    path = tmp_path_factory.mktemp("record") / "durations.json"
    path.write_text(json.dumps(durations))

    assert read_durations(path) == durations


@pytest.mark.parametrize(
    ("text", "culprit"),
    [
        ("", "not valid JSON"),
        ("[1, 2]", "JSON object"),
        ('{"tests/a.py": "3"}', "tests/a.py"),
        ('{"tests/a.py": -1}', "tests/a.py"),
        ('{"tests/a.py": true}', "tests/a.py"),
        ('{"tests/a.py": NaN}', "tests/a.py"),
        ('{"tests/a.py": null}', "tests/a.py"),
    ],
)
def test_a_malformed_record_is_rejected_naming_its_source_and_key(text: str, culprit: str) -> None:
    with pytest.raises(ValueError, match="cache-record") as raised:
        parse_durations(text, "cache-record")

    assert culprit in str(raised.value)


@given(first=_DURATIONS, second=_DURATIONS, counts=_ITEM_COUNTS, count=_COUNTS)
def test_a_refreshed_record_still_gives_every_test_exactly_one_owner(
    first: dict[str, float], second: dict[str, float], counts: dict[str, int], count: int
) -> None:
    """Shards that read a newer record than the checked-in one still cover each test once."""
    for written in (first, second):
        record = parse_durations(json.dumps(written), "record")
        whole, loads = shard_loads(counts, record, count)
        spread, _ = assign_heavy_items(counts, record, loads)

        for name, total in counts.items():
            if is_heavy(name, record, count):
                assert {spread[name, index] for index in range(total)} <= set(range(1, count + 1))
                assert name not in whole
            else:
                assert whole[name] in range(1, count + 1)
                assert (name, 0) not in spread


@given(durations=_DURATIONS, count=_COUNTS)
def test_a_file_is_spread_only_when_it_outweighs_a_shard(
    durations: dict[str, float], count: int
) -> None:
    mean_shard = sum(durations.values()) / count

    for name, seconds in durations.items():
        assert is_heavy(name, durations, count) == (seconds > HEAVY_SHARE * mean_shard)
    assert heavy_threshold(durations, count) == pytest.approx(HEAVY_SHARE * mean_shard)


def test_a_shard_that_owns_no_test_under_the_given_path_passes(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Path(__file__).parents[2]
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join((str(repository), os.environ.get("PYTHONPATH", "")))
    )
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    pytester.makeconftest((repository / "conftest.py").read_text(encoding="utf-8"))
    pytester.makepyfile(test_only="def test_only(): pass")
    durations = pytester.path / "durations.json"
    durations.write_text(json.dumps({"test_only.py": 1.0}))

    results = [
        pytester.runpytest_subprocess(
            "-n", "2", "--shard", f"{index}/2", "--shard-durations", str(durations), "test_only.py"
        )
        for index in (1, 2)
    ]

    assert [result.ret for result in results] == [0, 0]
    assert sorted(result.parseoutcomes().get("passed", 0) for result in results) == [0, 1]
