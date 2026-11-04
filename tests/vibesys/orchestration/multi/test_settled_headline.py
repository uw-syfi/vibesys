"""A round reports, ranks by and adopts the settled comparison's figure, never the first run's (#3027).

Core answers the framework's official check with the mean of the samples it holds at that moment,
and the runs a close call earns land after it and restate the ledger's record. The strategy waits
for the comparison to settle and reads the record as the ledger states it then, so the candidate
value that comparison states is the one figure an idea has: the one its round records (and
`round_finished` carries), its gate and the adoption are labelled with, and the best is chosen by.
These stories play the host of `multi`, whose record first states the first run's reading
(`throughputs`) and is restated to the settled comparison's value, and read the figure at each
place.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.multi._host import config
from tests.vibesys.serial_search._host import INPUT_THROUGHPUT
from tests.vibesys.vibesys_flows import across_restarts

from vs_sim.api.testing import examples

from .test_stories import design, implement, judge, selection_of, start

if TYPE_CHECKING:
    from tests.vibesys.orchestration.multi._host import MultiHost


NOISE = 0.01
RATIOS = st.floats(1.2, 3.0, allow_nan=False)
FIRST_RUNS = st.floats(20.0, 300.0, allow_nan=False)


def settled_value(ratio: float) -> float:
    """The candidate value the host's comparison states for a ratio over the input."""
    return INPUT_THROUGHPUT * ratio


def attrs_of(host: MultiHost, kind: str) -> list[dict[str, object]]:
    """The attributes of every span of ``kind`` the run labelled, in the order they started."""
    found: list[dict[str, object]] = []
    for span in host.run.trace().spans:
        label = span.label
        if label is not None and label.kind == kind and label.attrs_json:
            found.append(json.loads(label.attrs_json))
    return found


def play(
    *,
    restarting: bool,
    firsts: tuple[float, float],
    ratios: tuple[float, float],
) -> MultiHost:
    """Two rounds whose candidates read ``firsts`` on their first run and settle at ``ratios``."""
    host = start(
        config(max_rounds=2),
        restarting=restarting,
        throughputs={"c1": firsts[0], "c2": firsts[1]},
        comparisons={"c1": (ratios[0], NOISE), "c2": (ratios[1], NOISE)},
    )
    for number, name in enumerate(("c1", "c2"), start=1):
        design(host, f"h{number}")
        implement(host, candidate=name)
        judge(host, passed=True)
        if number == 1:
            host.drain_until_turn("orchestrator")
    host.drain()
    return host


def recorded(host: MultiHost) -> list[float | None]:
    record = host.record
    assert record is not None
    return [row.perf_metric for hypothesis in record.hypotheses for row in hypothesis.rounds]


@across_restarts
@settings(max_examples=examples(pr=10, nightly=60), deadline=None, derandomize=True, database=None)
@given(first=FIRST_RUNS, ratio=RATIOS)
def test_a_round_records_and_labels_the_settled_figure_not_its_first_runs(
    *, restarting: bool, first: float, ratio: float
) -> None:
    host = start(
        config(max_rounds=1),
        restarting=restarting,
        throughputs={"c1": first},
        comparisons={"c1": (ratio, NOISE)},
    )
    design(host)
    implement(host, candidate="c1")
    judge(host, passed=True)
    host.drain()

    assert recorded(host) == [settled_value(ratio)]
    (gate,) = attrs_of(host, "gate")
    assert gate["metric_value"] == settled_value(ratio)
    assert gate["vs_baseline"] == pytest.approx(ratio)


@across_restarts
def test_the_best_is_the_candidate_that_settled_highest_though_another_read_higher_first(
    *, restarting: bool
) -> None:
    """c1 read 100 on its first run and settled at 80; c2 read 90 and settled at 90: c2 is best."""
    host = play(restarting=restarting, firsts=(100.0, 90.0), ratios=(1.6, 1.8))

    assert selection_of(host) == ("retained_candidate", "c2")
    assert recorded(host) == [settled_value(1.6), settled_value(1.8)]
    adopted = attrs_of(host, "adoption")[-1]
    assert adopted["candidate"] == "c2"
    assert adopted["metric_value"] == settled_value(1.8)
    assert adopted["vs_baseline"] == pytest.approx(1.8)


@across_restarts
@settings(max_examples=examples(pr=10, nightly=60), deadline=None, derandomize=True, database=None)
@given(firsts=st.tuples(FIRST_RUNS, FIRST_RUNS), ratios=st.tuples(RATIOS, RATIOS))
def test_whatever_the_first_runs_read_the_adopted_figure_is_the_highest_settled_one(
    *, restarting: bool, firsts: tuple[float, float], ratios: tuple[float, float]
) -> None:
    host = play(restarting=restarting, firsts=firsts, ratios=ratios)

    assert recorded(host) == [settled_value(ratios[0]), settled_value(ratios[1])]
    best = max(ratios)
    adopted = attrs_of(host, "adoption")[-1]
    assert adopted["metric_value"] == settled_value(best)
    assert adopted["vs_baseline"] == pytest.approx(best)
    if ratios[0] != ratios[1]:
        assert selection_of(host)[1] == ("c1" if ratios[0] > ratios[1] else "c2")
