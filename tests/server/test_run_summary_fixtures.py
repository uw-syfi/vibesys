"""The shared run-summary fixtures: journals and the summary both sides must agree they fold to.

``tests/conformance/fixtures/run-summary/*.json`` hold ``events`` (a run's recorded events, each a
``RunEvent``; a fixture marked ``legacy`` was recorded by an older VibeSys, in a shape today's
models no longer accept, and is read as the journal it is) and the ``summary`` a client folds from them. This side checks that the helper's
fold equals it; ``clients/features/run-page`` checks that the client's own fold does, so the two
implementations of one definition cannot drift apart without a fixture failing.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from server.events import RunEvent
from server.run_digest import digest_journal
from server.run_summary_cache import summarize_journal

_FIXTURES = Path(__file__).parents[1] / "conformance" / "fixtures" / "run-summary"
_NAMES = sorted(path.name for path in _FIXTURES.glob("*.json"))


@pytest.mark.parametrize("name", _NAMES)
def test_the_helper_folds_a_fixture_journal_to_its_summary(name: str) -> None:
    fixture = json.loads((_FIXTURES / name).read_text())
    if not fixture.get("legacy", False):
        for event in fixture["events"]:
            RunEvent.model_validate(event)  # the fixture is a journal the run could have recorded
    journal = b"".join(
        json.dumps(event, separators=(",", ":")).encode() + b"\n" for event in fixture["events"]
    )

    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "run-events.jsonl"
        path.write_bytes(journal)
        cold = summarize_journal(path)
        warm = summarize_journal(path)  # from the cache the first read wrote

    for summarized in (cold, warm):
        state = summarized.fold.state()
        assert summarized.journal_bytes == len(journal)
        assert state.through_sequence == fixture["summary"]["through_sequence"]
        assert state.event_count == fixture["summary"]["event_count"]
        assert summarized.fold.trace.value == fixture["summary"]["trace"]
        assert state.rounds_finished == fixture["summary"]["rounds_finished"]
        assert list(state.spans) == fixture["summary"]["spans"]
        assert state.overview == fixture["summary"]["overview"]
        assert [
            {
                "span_id": reply.span_id,
                "kind": reply.kind,
                "json_schema": state.schemas[reply.schema_id],
                "value": reply.value,
            }
            for reply in state.replies
        ] == fixture["summary"]["replies"]
        assert state.tokens.model_dump() == fixture["summary"]["tokens"]
        assert [
            {"span_id": entry.span_id, "tally": entry.tally.model_dump()} for entry in state.usage
        ] == fixture["summary"]["usage"]


@pytest.mark.parametrize("name", _NAMES)
def test_a_list_row_and_the_run_it_opens_agree_whether_the_journal_has_ideas(name: str) -> None:
    fixture = json.loads((_FIXTURES / name).read_text())
    lines = [json.dumps(event).encode() + b"\n" for event in fixture["events"]]

    digest = digest_journal(lines)

    assert (digest.ideas is None) == (fixture["summary"]["trace"] == "predates_trace")


def test_a_journal_from_before_the_trace_has_rounds_and_an_honest_end_but_no_ideas() -> None:
    fixture = json.loads((_FIXTURES / "predates-trace.json").read_text())

    digest = digest_journal([json.dumps(event).encode() + b"\n" for event in fixture["events"]])

    assert digest.ideas is None
    assert digest.closing is not None
    assert digest.closing.value == "finished"
    assert digest.adopted_ratio is None
