"""Which crash points a run offers: a sample or every one, from one setting."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_dst.api import (
    EVERY_POINT,
    SAMPLE_SIZE,
    SAMPLED,
    CrashKind,
    CrashPoint,
    CrashPointPolicy,
    crash_point_policy,
    simulate,
    using_crash_point_policy,
)

from ._tiny import TinyWorld

pytest_plugins = ["pytester"]
PLUGIN_DIR = Path(__file__).resolve().parents[1] / "pytest_plugin"


@given(
    commits=st.integers(min_value=0, max_value=300), per_kind=st.integers(min_value=1, max_value=40)
)
def test_a_sample_has_the_first_and_last_commit_and_at_most_the_asked_number_per_kind(
    commits: int, per_kind: int
) -> None:
    points = CrashPointPolicy(per_kind).choose(commits)

    for kind in CrashKind:
        indexes = [point.commit for point in points if point.kind is kind]
        assert len(indexes) == min(per_kind, commits)
        assert indexes == sorted(set(indexes))
        if commits:
            assert indexes[0] == 0
            assert per_kind == 1 or indexes[-1] == commits - 1


@given(commits=st.integers(min_value=0, max_value=100))
def test_every_point_policy_offers_both_kinds_of_every_commit(commits: int) -> None:
    points = EVERY_POINT.choose(commits)

    assert set(points) == {CrashPoint(kind, n) for kind in CrashKind for n in range(commits)}


def test_a_run_offers_a_sample_by_default_and_every_point_when_the_setting_says_all() -> None:
    normal = simulate(TinyWorld(2))
    assert normal.commits > SAMPLE_SIZE

    assert crash_point_policy() == SAMPLED
    assert len(normal.crash_points()) == 2 * SAMPLE_SIZE
    with using_crash_point_policy(EVERY_POINT):
        assert len(normal.crash_points()) == 2 * normal.commits
    assert len(normal.crash_points()) == 2 * SAMPLE_SIZE


def test_a_run_offers_the_crash_points_of_one_kind_when_asked() -> None:
    normal = simulate(TinyWorld(1))

    for kind in CrashKind:
        points = normal.crash_points(kind)
        assert points
        assert {point.kind for point in points} == {kind}
    assert normal.crash_points() == (
        normal.crash_points(CrashKind.BEFORE_COMMIT) + normal.crash_points(CrashKind.AFTER_COMMIT)
    )


def test_a_crash_point_reads_as_where_the_host_dies() -> None:
    point = CrashPoint(CrashKind.BEFORE_COMMIT, 17)

    assert str(point) == "before commit 17"
    assert repr(CrashPoint(CrashKind.AFTER_COMMIT, 3)) == "after commit 3"


def test_a_point_that_is_not_one_of_the_run_is_refused_by_name() -> None:
    normal = simulate(TinyWorld(1))

    with pytest.raises(ValueError, match="after commit 9999 is not a crash point"):
        normal.rerun_with_crash(CrashPoint(CrashKind.AFTER_COMMIT, 9999))


def test_a_recovered_run_offers_the_crash_points_of_its_own_hosts_and_can_be_crashed_again() -> (
    None
):
    normal = simulate(TinyWorld(2))
    first = normal.crash_points()[len(normal.crash_points()) // 2]
    recovered = normal.rerun_with_crash(first)

    points = recovered.crash_points()

    assert points
    assert all(point.commit > first.commit for point in points)
    for second in points:
        again = recovered.rerun_with_crash(second)
        assert again.finished, (first, second)
        assert again.result == normal.result, (first, second)
        assert again.no_effect_ran_twice(), (first, second)
        assert {d.decision_id for d in again.decisions} == {
            d.decision_id for d in normal.decisions
        }, (first, second)
    with pytest.raises(ValueError, match="is not a crash point"):
        recovered.rerun_with_crash(first)


def test_a_recovered_run_reads_back_the_world_it_left() -> None:
    normal = simulate(TinyWorld(2))

    recovered = normal.rerun_with_crash(normal.crash_points()[3])

    assert recovered.world is not normal.world
    assert type(recovered.world) is TinyWorld


@pytest.fixture
def suite(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> pytest.Pytester:
    """A scratch project that loads the plugin, as the repository's root conftest does."""
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join(filter(None, [str(PLUGIN_DIR), os.environ.get("PYTHONPATH")]))
    )
    pytester.makeini("[pytest]\naddopts = -p vs_dst_pytest -p no:cacheprovider\n")
    pytester.makepyfile(
        """
        from vs_dst.api import EVERY_POINT, SAMPLED, crash_point_policy

        def test_policy():
            print("POLICY", "all" if crash_point_policy() == EVERY_POINT else "sampled")
        """
    )
    return pytester


@pytest.mark.parametrize(
    ("arguments", "chosen"), [([], "sampled"), (["--dst-crash-points=all"], "all")]
)
def test_the_pytest_option_sets_the_one_policy_of_the_session(
    suite: pytest.Pytester, arguments: list[str], chosen: str
) -> None:
    result = suite.runpytest_subprocess("-s", *arguments)

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines([f"*POLICY {chosen}*"])
