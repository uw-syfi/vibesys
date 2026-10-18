"""The shared run-digest fixtures: journals and the figures a list row and the run page must agree on.

``tests/conformance/fixtures/run-digest/*.json`` hold ``events`` (a run's recorded events) and the
``digest`` (its ideas and adopted ratio) that the run page reads from them. This side checks that
the helper's digest equals it; ``clients/features/run-page`` checks that the page's own fold does
(``run-digest-conformance.test.ts``), so a row of the sidebar and the page it opens cannot say
different things about one run without a fixture failing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.events import RunEvent
from server.run_digest import digest_journal

_FIXTURES = Path(__file__).parents[1] / "conformance" / "fixtures" / "run-digest"
_NAMES = sorted(path.name for path in _FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", _NAMES)
def test_the_helpers_digest_of_a_fixture_journal_is_the_figures_the_page_reads(name: str) -> None:
    fixture = json.loads((_FIXTURES / name).read_text())
    for event in fixture["events"]:
        RunEvent.model_validate(event)  # the fixture is a journal the run could have recorded

    digest = digest_journal([json.dumps(event).encode() + b"\n" for event in fixture["events"]])

    assert digest.ideas == fixture["digest"]["ideas"]
    assert digest.adopted_ratio == pytest.approx(fixture["digest"]["adopted_ratio"])
    assert digest.best_so_far_ratio == pytest.approx(fixture["digest"].get("best_so_far_ratio"))


def test_the_corpus_holds_a_run_whose_best_reading_belongs_to_a_failed_idea() -> None:
    fixture = json.loads((_FIXTURES / "failed-best.json").read_text())
    readings = [
        json.loads(json.loads(line)["data"]["span"]["label"]["attrs_json"]).get("vs_baseline")
        for line in map(json.dumps, fixture["events"])
        if '"gate"' in line
    ]

    assert (
        max(reading for reading in readings if reading is not None)
        > fixture["digest"]["adopted_ratio"]
    )
