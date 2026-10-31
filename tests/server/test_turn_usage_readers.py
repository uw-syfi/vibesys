"""Every reader of a journal's ``turn_usage_recorded`` lines takes a line with the per-model split and one without.

A line carries ``cost_basis``, ``by_model`` and ``provider_session_id`` from the version that added
them; every line before has none, and journals are not migrated. Both shapes live on one host at
once, so each reader below must accept both. A reader that rejects the new shape fails a live
run's summary or the Run page, and one that rejects the old shape fails every run recorded before:
the summary fold does not raise, it stops where the journal "ends", so a rejected line drops the
rest of the run's history without an error.

The readers are listed here by name. A new reader of these lines belongs in this list; the list is
what "every reader" means, and the TypeScript readers are held by the shared usage-record fixtures
(``usage-record-conformance.test.ts``) and the event corpus.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from tests.support.usage_events import split_usage_lines

import server.events as wire
import vibesys.events as core
import vs_proto.api as p
from server.run_summary import RunSummaryFold
from server.run_summary_cache import summarize_journal
from server.run_usage import usage_records
from vibesys.api.importing import ImportSource, SourceFormat, import_run_facts
from vibesys.records.api import MappingState, map_event
from vs_core.api.testing import FAKE_REGISTRY
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_CORPUS = Path(__file__).parents[1] / "conformance" / "events" / "turn_usage_recorded.json"
_SPLIT_KEYS = ("cost_basis", "by_model", "provider_session_id")


def _with_split() -> dict[str, Any]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))


def _without_split() -> dict[str, Any]:
    line = _with_split()
    line["data"] = {key: value for key, value in line["data"].items() if key not in _SPLIT_KEYS}
    return line


def _core_event(line: Mapping[str, Any]) -> core.CoreEvent:
    fields = {name: value for name, value in line.items() if name in core.CoreEvent.model_fields}
    return core.CoreEvent.model_validate(fields)


def _read_core_event(line: Mapping[str, Any]) -> None:
    assert isinstance(_core_event(line).data, core.TurnUsageData)


def _read_wire_event(line: Mapping[str, Any]) -> None:
    assert isinstance(wire.RunEvent.model_validate(line).data, wire.TurnUsageData)


def _read_core_event_as_wire_event(line: Mapping[str, Any]) -> None:
    """The server revalidates a core payload as the wire's, which forbids keys it does not declare."""
    data = _core_event(line).data
    assert isinstance(data, core.TurnUsageData)
    revalidated = wire.TurnUsageData.model_validate(data.model_dump(mode="python"))
    assert revalidated.model_dump(mode="json") == data.model_dump(mode="json")


def _read_usage_fold(line: Mapping[str, Any]) -> None:
    assert len(usage_records([line])) == 1


def _read_summary_fold(line: Mapping[str, Any]) -> None:
    fold = RunSummaryFold()
    assert fold.consume(json.dumps(line).encode() + b"\n") is True
    assert fold.record_count == 1


def _read_summary_cache(line: Mapping[str, Any]) -> None:
    """Fold a journal file, write its cache, then read the cache back."""
    with tempfile.TemporaryDirectory() as directory:
        journal = Path(directory) / "run-events.jsonl"
        journal.write_bytes(json.dumps(line).encode() + b"\n")
        cold = summarize_journal(journal)
        warm = summarize_journal(journal)
    assert len(cold.fold.state().records) == 1
    assert warm.fold.state().records == cold.fold.state().records


def _read_records_projector(line: Mapping[str, Any]) -> None:
    result = map_event(MappingState("run-conformance"), _core_event(line))
    for record in result.records:
        p.validate(record)


def _read_fact_import(line: Mapping[str, Any]) -> None:
    """The import reads the core journal: no protocol version, and a complete one from sequence 1."""
    recorded = {
        **{name: value for name, value in line.items() if name in core.CoreEvent.model_fields},
        "sequence": 1,
    }
    source = ImportSource(
        "core-events.jsonl", SourceFormat.EVENTS, json.dumps(recorded).encode() + b"\n"
    )
    facts = import_run_facts(
        "run-conformance", (source,), strategy="single", registry=FAKE_REGISTRY
    )
    assert any(fact.value is not None and fact.value.field == "usage" for fact in facts.facts)


READERS: dict[str, Callable[[Mapping[str, Any]], None]] = {
    "the core journal's event": _read_core_event,
    "the wire's run event": _read_wire_event,
    "the core payload revalidated as the wire's": _read_core_event_as_wire_event,
    "the usage fold": _read_usage_fold,
    "the run summary fold": _read_summary_fold,
    "the run summary cache": _read_summary_cache,
    "the records projector": _read_records_projector,
    "the retained-run fact import": _read_fact_import,
}


@pytest.mark.parametrize("reader", READERS.values(), ids=READERS.keys())
@pytest.mark.parametrize(
    "shape", [_with_split, _without_split], ids=["with the split", "without the split"]
)
def test_every_reader_takes_a_usage_line_with_the_split_and_one_without(
    reader: Callable[[Mapping[str, Any]], None], shape: Callable[[], dict[str, Any]]
) -> None:
    reader(shape())


def test_the_two_shapes_differ_in_exactly_the_split() -> None:
    with_split, without_split = _with_split(), _without_split()

    assert set(with_split["data"]) - set(without_split["data"]) == set(_SPLIT_KEYS)
    assert without_split["data"].keys() <= with_split["data"].keys()


@settings(max_examples=examples(pr=30, nightly=600), deadline=None)
@given(data=split_usage_lines())
def test_every_reader_takes_any_split_a_producer_could_write(data: dict[str, Any]) -> None:
    line = {**_with_split(), "data": data["data"]}
    for reader in READERS.values():
        reader(line)


@pytest.mark.parametrize("name", sorted(_SPLIT_KEYS))
def test_the_wire_declares_every_split_field_the_core_writes(name: str) -> None:
    assert name in core.TurnUsageData.model_fields
    assert name in wire.TurnUsageData.model_fields
    assert (
        core.ModelTurnUsageData.model_fields.keys() == wire.ModelTurnUsageData.model_fields.keys()
    )
