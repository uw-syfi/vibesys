"""A merge-queue failure is explained, never retried: each failed test is labelled by main's result."""

from __future__ import annotations

import json

import pytest
from hypothesis import given
from hypothesis import strategies as st
from scripts.classify_failed_tests import MAX_RERUN, Outcome, classify, failed_ids, summary

NODES = st.text(alphabet="abcdefgh/_.:[]-0123456789", min_size=1, max_size=24)


@given(failed=st.sets(NODES, max_size=12), on_main=st.sets(NODES, max_size=12))
def test_every_failed_test_gets_exactly_one_label_decided_by_main(
    failed: set[str], on_main: set[str]
) -> None:
    ordered = sorted(failed)
    outcomes = classify(ordered, on_main)
    assert list(outcomes) == ordered
    for node, outcome in outcomes.items():
        assert (outcome is Outcome.FAILS_ON_MAIN) == (node in on_main)


@given(failed=st.sets(NODES, min_size=1, max_size=8), on_main=st.sets(NODES, max_size=8))
def test_the_summary_lists_each_test_once_under_its_label(
    failed: set[str], on_main: set[str]
) -> None:
    outcomes = classify(sorted(failed), on_main)
    text = summary(outcomes, base="abc123")
    assert Outcome.FAILS_ON_MAIN.value in text
    assert Outcome.FAILS_ONLY_WITH_THE_PR.value in text
    for node, outcome in outcomes.items():
        section = text.split(f"### {outcome.value}")[1].split("###")[0]
        assert section.count(f"- `{node}`") == 1
    assert "unchanged" in text


def test_a_test_that_passes_on_main_is_the_pull_requests_own_failure() -> None:
    outcomes = classify(["tests/a.py::test_x"], failed_on_main=[])
    assert outcomes == {"tests/a.py::test_x": Outcome.FAILS_ONLY_WITH_THE_PR}


def test_the_labels_are_the_ones_authors_read() -> None:
    assert Outcome.FAILS_ON_MAIN.value == "fails on main (flaky or broken main)"
    assert Outcome.FAILS_ONLY_WITH_THE_PR.value == "fails only with this PR"


@given(shards=st.lists(st.dictionaries(NODES, st.booleans(), max_size=6), min_size=1, max_size=3))
def test_failed_ids_are_the_true_entries_of_every_shard_sorted_and_unique(
    shards: list[dict[str, bool]],
) -> None:
    got = failed_ids(json.dumps(shard) for shard in shards)
    assert got == sorted({node for shard in shards for node, failed in shard.items() if failed})


def test_a_cache_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(TypeError, match="JSON object"):
        failed_ids(["[1, 2]"])


def test_failures_past_the_cap_are_reported_as_not_rerun() -> None:
    text = summary({}, base="abc", skipped=3)
    assert "3 more failed tests were not rerun" in text
    assert str(MAX_RERUN) in text
