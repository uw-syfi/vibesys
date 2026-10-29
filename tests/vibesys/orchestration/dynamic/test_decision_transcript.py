"""Every decision the dynamic flow makes over fixed scenarios, pinned by id and payload digest.

Decision ids and payloads are persisted in the run's journal, and core deduplicates a replayed
decision by both; a restart under changed code resumes flows from their checkpoints. A refactor
of the flow's building blocks must therefore not change a single byte of what it asks for. The
golden file was recorded when this test was written; a change to it is a change of the persisted
contract and needs a reason of its own (record it again with
``python -m tests.vibesys.orchestration.dynamic.test_decision_transcript``).
"""

from __future__ import annotations

import hashlib
import itertools
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._host import PLANNER_LABEL, Host, begin, config
from tests.vibesys.orchestration.dynamic._replies import implement, plan_reply

from vs_core.api import Finish, Wait

if TYPE_CHECKING:
    from vs_core.api import Decision

_GOLDEN = Path(__file__).with_name("golden_decision_transcripts.json")
_REVISIONS = tuple(f"c{n}" for n in range(1, 16))


def _scenarios() -> dict[str, dict[str, object]]:
    """A fixed, ordered grid: portfolio size, parallelism, review, repair, benchmark."""
    grid = itertools.product(
        (1, 2, 3),  # hypotheses in the plan
        (1, 2),  # max_in_flight
        (True, False),  # every review passes
        (False, True),  # the plan reply needs one correction first
        (100.0, 10.0),  # benchmark: beats the input, or does not
    )
    return {
        f"h{count}-p{parallel}-{'pass' if passes else 'fail'}"
        f"-{'fix' if fix else 'ok'}-b{int(bench)}": {
            "count": count,
            "parallel": parallel,
            "passes": passes,
            "fix": fix,
            "bench": bench,
        }
        for count, parallel, passes, fix, bench in grid
    }


def _play(spec: dict[str, object]) -> Host:
    count, parallel = int(str(spec["count"])), int(str(spec["parallel"]))
    host = begin(
        config(max_rounds=1, max_in_flight=parallel, evaluation_slots=parallel),
        restarting=False,
        throughputs={revision: float(str(spec["bench"])) for revision in _REVISIONS},
    )
    plans = [plan_reply(*(implement(f"h{n}") for n in range(count)))]
    if spec["fix"]:
        plans.insert(0, "not json")
    for _ in range(500):
        answerable = [item for item in host.run.open if item.until is not None]
        if not answerable:
            if not host.run.sleeping_until:
                break
            host.run.advance_to(min(host.run.sleeping_until))
            continue
        asked = answerable[0]
        if asked.label == PLANNER_LABEL and plans:
            host.run.reply_raw(asked, plans.pop(0))
        elif asked.label == "judge":
            host.answer(asked, 0 if spec["passes"] else 1)
        elif asked.label == PLANNER_LABEL:
            host.answer(asked, 3)  # a later planning call times out
        else:
            host.answer(asked, 0)
    return host


def _named(item: Decision) -> str:
    """The id a decision carries; a wake-up has none, so only its payload is pinned."""
    if isinstance(item, Wait):
        return item.request.decision_id.root
    return item.decision_id.root if isinstance(item, Finish) else ""


def _transcript(spec: dict[str, object]) -> list[list[str]]:
    host = _play(spec)
    assert host.run.finished is not None, host.run.timeline()
    assert not host.unsettled
    return [
        [
            type(item).__name__,
            _named(item),
            hashlib.sha256(item.model_dump_json().encode()).hexdigest()[:16],
        ]
        for item in host.run.decisions
    ]


def _record() -> dict[str, list[list[str]]]:
    return {name: _transcript(spec) for name, spec in _scenarios().items()}


@pytest.mark.parametrize("name", list(_scenarios()))
def test_decisions_are_identical_to_the_recorded_transcript(name: str) -> None:
    golden = json.loads(_GOLDEN.read_text())
    assert _transcript(_scenarios()[name]) == golden[name]


def test_the_transcript_of_a_scenario_is_the_same_every_time_it_is_played() -> None:
    spec = next(iter(_scenarios().values()))
    assert _transcript(spec) == _transcript(spec)


if __name__ == "__main__":
    _GOLDEN.write_text(json.dumps(_record(), indent=0, sort_keys=True) + "\n")
