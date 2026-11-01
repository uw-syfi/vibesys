"""A run's new bests are every idea that became the best so far, however many the run tried.

The digest's dashboard keeps 64 ideas, so counting new bests from it drops the early bests of a
long run without saying so. The rollup reads the same fold of the same journal without the cap.
The oracle is the rule itself: an idea that passed and beat every passed idea before it, and the
input, became the best at the time its measurement landed.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.usage_support import Projects, write_run

from server.host_run_dashboard import DASHBOARD_MAX_POINTS, kept_landings
from server.run_digest import digest_journal, journal_kept_landings
from server.usage_rollup import Budget
from server.usage_scan import collect_runs

BASE = datetime(2026, 10, 17, 3, tzinfo=UTC)
RUN_ID = "20261010-120000-11111111-dynamic"


@dataclass(frozen=True)
class _Measured:
    """An idea measured at ``value`` against an input of 100, and passed or failed."""

    passed: bool
    value: float


def _event(sequence: int, kind: str, at: float, data: dict[str, object]) -> bytes:
    document = {
        "protocol_version": 1,
        "sequence": sequence,
        "run_id": RUN_ID,
        "timestamp": (BASE + timedelta(seconds=at)).isoformat().replace("+00:00", "Z"),
        "type": kind,
        "text": "",
        "data": {"kind": kind, **data},
    }
    return json.dumps(document).encode() + b"\n"


def _label(kind: str, verdict: str | None, attrs: dict[str, object]) -> dict[str, object]:
    return {"kind": kind, "verdict": verdict, "attrs_json": json.dumps(attrs)}


def _journal(ideas: list[_Measured], direction: str) -> list[bytes]:
    """The run starts and plays ``ideas``: idea ``n`` is a workstream whose gate read its value at second ``n + 1``."""
    lines = [_event(1, "run_started", 0, {"overview": {"objective": {"direction": direction}}})]
    for index, idea in enumerate(ideas):
        attrs: dict[str, object] = {"hypothesis_id": f"h{index}", "revision": f"rev-{index}"}
        started = {"id": f"flow:{index}", "label": _label("workstream", None, attrs)}
        lines.append(_event(len(lines) + 1, "span_started", 1 + index, {"span": started}))
        gate_attrs: dict[str, object] = {
            "metric_value": idea.value,
            "vs_baseline": idea.value / 100,
        }
        gate = {
            "id": f"op:{index}",
            "parent": f"flow:{index}",
            "label": _label("gate", "passed", gate_attrs),
        }
        lines.append(_event(len(lines) + 1, "span_ended", 1 + index, {"span": gate}))
        verdict = "passed" if idea.passed else "failed"
        ended = {"id": f"flow:{index}", "label": _label("workstream", verdict, attrs)}
        lines.append(_event(len(lines) + 1, "span_ended", 1 + index, {"span": ended}))
    return lines


def _expected(ideas: list[_Measured], direction: str) -> list[datetime]:
    """The ideas that became the best so far, by the rule, with the time each landed."""
    standing = 100.0
    landed: list[datetime] = []
    for index, idea in enumerate(ideas):
        better = idea.value > standing if direction == "max" else idea.value < standing
        if idea.passed and better:
            standing = idea.value
            landed.append(BASE + timedelta(seconds=1 + index))
    return landed


_IDEAS = st.lists(
    st.builds(
        _Measured,
        st.booleans(),
        # The digest rebuilds the input's value from a ratio, so a reading within rounding of
        # it is a tie there, and is not drawn.
        st.floats(20, 500, allow_nan=False).filter(lambda value: abs(value - 100) > 1e-6),
    ),
    max_size=150,
)
_CLIMB = st.integers(min_value=DASHBOARD_MAX_POINTS + 1, max_value=DASHBOARD_MAX_POINTS * 3)


@given(ideas=_IDEAS, direction=st.sampled_from(["max", "min"]))
def test_the_new_bests_of_a_journal_are_the_ideas_that_became_the_best(
    ideas: list[_Measured], direction: str
) -> None:
    lines = _journal(ideas, direction)
    assert list(journal_kept_landings(lines)) == _expected(ideas, direction)


@example(count=DASHBOARD_MAX_POINTS * 2)
@given(count=_CLIMB)
def test_a_run_with_more_bests_than_the_dashboard_holds_loses_none(count: int) -> None:
    """Every idea beats the last: all are bests. The dashboard's points are capped; the count is not."""
    ideas = [_Measured(passed=True, value=101.0 + index) for index in range(count)]
    lines = _journal(ideas, "max")
    dashboard = digest_journal(lines).dashboard
    assert dashboard is not None
    capped = kept_landings(dashboard.points)
    assert len(capped) < count
    assert len(journal_kept_landings(lines)) == count


def test_a_rollup_holds_every_best_of_a_long_run() -> None:
    count = DASHBOARD_MAX_POINTS + 36
    ideas = [_Measured(passed=True, value=101.0 + index) for index in range(count)]
    with tempfile.TemporaryDirectory() as raw:
        project = Path(raw)
        write_run(project, RUN_ID, b"".join(_journal(ideas, "max")))
        collected = collect_runs(
            Projects(), [project], since=BASE - timedelta(days=1), budget=Budget(10**9)
        )
        (run,) = collected.runs
        assert len(run.rollup.new_bests) == count
