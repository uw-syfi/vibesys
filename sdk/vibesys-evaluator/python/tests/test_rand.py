"""``Rand``: the Go generators' streams exactly, deterministic, labelled children."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys_evaluator import Rand

if TYPE_CHECKING:
    from collections.abc import Callable

# Drawn by the Go generators' newRNG(seed, label, index) (evaluator/internal/dataset/random.go of
# the DeathStarBench tasks): next(), intn(1000), float(), normal(), pick(the stay weights).
GO_STREAMS = [
    (7, "user", 3, 2938209492885495702, 222, 0.023316435496805177, 1.5368528106897494, 0),
    (407708418465, "post", 0, 3736897710123859747, 726, 0.7653362506708002, -1.4428586868625035, 0),
    (42, "hotel", 12345, 8923249924314765577, 755, 0.30174024768901, -1.3852759929859078, 1),
    (0, "", 0, 2448385507222971125, 636, 0.7037744320524411, -1.8374364599238993, 0),
]
STAY_WEIGHTS = [30, 24, 16, 11, 8, 6, 5]
# NewZipf(1000, ZipfThreeQuarters).Rank and NewZipf(20, ZipfOne).Rank of newRNG(9, "zipf", 1).float().
GO_ZIPF = [(474, 7), (9, 1), (289, 3), (301, 5), (970, 0), (15, 1)]

seeds = st.integers(min_value=0, max_value=(1 << 64) - 1)
labels = st.lists(
    st.one_of(st.text(max_size=8), st.integers(min_value=0, max_value=1 << 40)), max_size=4
)


@pytest.mark.parametrize("stream", GO_STREAMS, ids=lambda s: f"{s[1] or 'empty'}-{s[2]}")
def test_a_stream_draws_what_the_go_generator_draws(stream: tuple) -> None:
    seed, label, index, raw, below, unit, normal, pick = stream
    rand = Rand(seed, label, index)
    assert rand.below(1 << 64) == raw
    assert rand.below(1000) == below
    assert rand.random() == unit
    assert rand.normal() == normal
    assert rand.weighted(STAY_WEIGHTS) == pick


def test_zipf_ranks_are_the_go_samplers() -> None:
    rand = Rand(9, "zipf", 1)
    assert [(rand.zipf(1000, 0.75), rand.zipf(20, 1.0)) for _ in GO_ZIPF] == GO_ZIPF


@given(seeds, labels)
def test_a_stream_is_a_function_of_its_seed_and_labels(seed: int, names: list[str | int]) -> None:
    a, b = Rand(seed, *names), Rand(seed, *names)
    assert [a.random() for _ in range(5)] == [b.random() for _ in range(5)]


@given(seeds, labels, labels, st.integers(min_value=0, max_value=20))
def test_a_child_is_the_stream_of_the_joined_labels_whatever_was_drawn(
    seed: int, parent: list[str | int], more: list[str | int], drawn: int
) -> None:
    rand = Rand(seed, *parent)
    for _ in range(drawn):
        rand.random()
    child, direct = rand.child(*more), Rand(seed, *parent, *more)
    assert [child.below(1 << 64) for _ in range(3)] == [direct.below(1 << 64) for _ in range(3)]


@given(seeds, st.text(max_size=6), st.integers(min_value=0, max_value=1000))
def test_neighbouring_labels_name_different_streams(seed: int, label: str, index: int) -> None:
    streams = {
        Rand(seed, label, index).below(1 << 64),
        Rand(seed, label, index + 1).below(1 << 64),
        Rand(seed, label).below(1 << 64),
        Rand(seed, label + "x", index).below(1 << 64),
    }
    assert len(streams) == 4


@given(seeds, st.integers(min_value=1, max_value=1 << 70))
def test_below_stays_below(seed: int, n: int) -> None:
    rand = Rand(seed)
    assert all(0 <= rand.below(n) < n for _ in range(20))


@given(seeds)
def test_random_is_in_the_unit_interval_and_normal_is_bounded(seed: int) -> None:
    rand = Rand(seed)
    assert all(0.0 <= rand.random() < 1.0 for _ in range(20))
    assert all(abs(rand.normal()) <= 2 * 3**0.5 for _ in range(20))


@given(
    seeds,
    st.lists(st.one_of(st.integers(0, 50), st.floats(0, 50)), min_size=1, max_size=10).filter(
        lambda w: sum(w) > 0
    ),
)
def test_weighted_never_draws_a_zero_weight(seed: int, weights: list[float]) -> None:
    rand = Rand(seed)
    for _ in range(30):
        index = rand.weighted(weights)
        assert 0 <= index < len(weights)
        assert weights[index] > 0


@given(
    seeds,
    st.integers(min_value=1, max_value=500),
    st.sampled_from([0.0, 0.5, 0.75, 0.99, 1.0, 1.3]),
)
def test_zipf_ranks_are_in_range(seed: int, n: int, s: float) -> None:
    rand = Rand(seed)
    assert all(0 <= rand.zipf(n, s) < n for _ in range(20))


def test_zipf_favours_low_ranks() -> None:
    rand = Rand(3, "zipf")
    draws = [rand.zipf(100, 1.0) for _ in range(2000)]
    assert draws.count(0) > draws.count(1) > draws.count(10)


@pytest.mark.parametrize(
    ("call", "error"),
    [
        pytest.param(lambda: Rand(1, 1.5), TypeError, id="float-label"),  # ty: ignore[invalid-argument-type]
        pytest.param(lambda: Rand(1, -1), ValueError, id="negative-label"),
        pytest.param(lambda: Rand(1).below(0), ValueError, id="below-zero"),
        pytest.param(lambda: Rand(1).weighted([0, 0]), ValueError, id="zero-weights"),
        pytest.param(lambda: Rand(1).weighted([]), ValueError, id="no-weights"),
        pytest.param(lambda: Rand(1).zipf(0, 1.0), ValueError, id="zipf-empty"),
    ],
)
def test_invalid_arguments_are_refused(call: Callable[[], object], error: type[Exception]) -> None:
    with pytest.raises(error):
        call()
