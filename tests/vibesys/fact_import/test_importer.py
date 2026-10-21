"""Retained formats lower to validated facts without executing their saved strategies."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.api.importing import (
    FactImportError,
    ImportSource,
    SourceFormat,
    import_run_facts,
    read_run_facts,
)
from vs_core.api import FlowState, RunEnvelope
from vs_core.api.testing import FAKE_REGISTRY
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    StoredEnvelope,
)
from vs_project.api.testing import run_execution_record
from vs_proto.api import imported_facts as pb
from vs_proto.api import validate
from vs_runtime.api.core import RuntimeRecord

_FIXTURES = Path(__file__).with_name("fixtures")


def _import(*sources: ImportSource, strategy: str = "single") -> pb.ImportedRunFacts:
    return import_run_facts("run", sources, strategy=strategy, registry=FAKE_REGISTRY)


def _envelope() -> dict[str, Any]:
    return json.loads((_FIXTURES / "envelope-v42.json").read_bytes())


def _sources() -> tuple[ImportSource, ...]:
    envelope = FAKE_REGISTRY.decode_envelope(RunEnvelope[FlowState], json.dumps(_envelope()))
    sequence = envelope.event_cursor.sequence
    runtime = RuntimeRecord[FlowState](
        schema_version=1,
        envelope=envelope,
        pending_publications=(),
        delivery_cursor=sequence,
        next_publication_sequence=sequence + 1,
        pending_inputs=(),
    )
    payload = runtime.model_dump_json().encode()
    stored = StoredEnvelope(revision=0, schema_version=1, payload=payload).model_dump(mode="json")
    return (
        ImportSource(
            "envelope.json", SourceFormat.ENVELOPE, (_FIXTURES / "envelope-v42.json").read_bytes()
        ),
        ImportSource("flow.json", SourceFormat.FLOW, envelope.strategy.model_dump_json().encode()),
        ImportSource("runtime.json", SourceFormat.RUNTIME, payload),
        ImportSource(
            "store-v1.json",
            SourceFormat.PROJECT_STORE,
            json.dumps({"version": 1, "record": stored}).encode(),
        ),
        ImportSource(
            "store-v2.json",
            SourceFormat.PROJECT_STORE,
            json.dumps({"version": 2, "record": stored}).encode(),
        ),
        ImportSource(
            "publications.json",
            SourceFormat.PUBLICATIONS,
            b'{"schema_version":1,"publications":[]}',
        ),
    )


def _payloads(result: pb.ImportedRunFacts, kind: str) -> list[Any]:
    return [
        fact.value.value
        for fact in result.facts
        if fact.value is not None and fact.value.field == kind
    ]


def test_all_current_containers_preserve_the_same_measurements_and_provenance() -> None:
    sources = _sources()
    single = _import(sources[0])
    imported = _import(*sources)
    assert _payloads(single, "measurement") == _payloads(imported, "measurement")
    samples = [row.sample for row in _payloads(imported, "measurement") if row.sample is not None]
    assert samples
    assert all(sample.measured_input and sample.evaluation and sample.host for sample in samples)
    assert all(fact.imported_from for fact in imported.facts)
    assert {origin.path for fact in imported.facts for origin in fact.imported_from} >= {
        source.path for source in sources[:-1]
    }
    assert imported.to_binary() == _import(*reversed(sources)).to_binary()
    validate(pb.ImportedRunFacts.from_binary(imported.to_binary()))


@pytest.mark.parametrize("name", ["completed", "suspended_evaluation", "empty"])
def test_legacy_loop_fixtures_import_without_upgrading_their_snapshots(name: str) -> None:
    data = (_FIXTURES / f"legacy-v6-{name}.json").read_bytes()
    source = ImportSource(f"dynamic/{name}.json", SourceFormat.LEGACY_DYNAMIC, data)
    content = ImportSource("objective.md", SourceFormat.CONTENT, b"Improve throughput.")
    result = _import(source, content, strategy="dynamic")
    assert source.data == data
    validate(result)
    if name != "empty":
        assert _payloads(result, "candidate")
        assert _payloads(result, "measurement")
        assert _payloads(result, "usage")
    assert all(
        origin.sha256 == sha256(data).hexdigest()
        for fact in result.facts
        for origin in fact.imported_from
        if origin.path == source.path
    )


@given(st.permutations(_sources()))
def test_import_is_deterministic_over_source_order(sources: tuple[ImportSource, ...]) -> None:
    assert _import(*sources).to_binary() == _import(*_sources()).to_binary()


@given(data=st.binary(max_size=2048), source_format=st.sampled_from(tuple(SourceFormat)))
def test_generated_stores_return_validated_facts_or_a_named_prerequisite(
    data: bytes, source_format: SourceFormat
) -> None:
    source = ImportSource("generated/store.json", source_format, data)
    try:
        result = _import(source)
    except FactImportError as error:
        outcome = error
    else:
        outcome = result
    if isinstance(outcome, FactImportError):
        assert "generated/store.json" in str(outcome) or outcome.path == "run"
    else:
        validate(outcome)
        assert outcome == _import(source)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_measurements_are_rejected_with_the_source_path(value: float) -> None:
    envelope = _envelope()
    envelope["core"]["evaluation"]["samples"][0]["reading"]["value"] = value
    with pytest.raises(FactImportError, match=r"damaged.json.*samples.*value"):
        _import(ImportSource("damaged.json", SourceFormat.ENVELOPE, json.dumps(envelope).encode()))


@pytest.mark.parametrize("field", ["unknown", "snapshot_upgrade", "scheduler"])
def test_unknown_retained_keys_are_rejected(field: str) -> None:
    envelope = _envelope()
    envelope["strategy"]["live"][field] = True
    with pytest.raises(FactImportError, match=field):
        _import(ImportSource("damaged.json", SourceFormat.ENVELOPE, json.dumps(envelope).encode()))


def test_conflicting_immutable_sample_id_fails_instead_of_picking_one_source() -> None:
    other = _envelope()
    other["core"]["evaluation"]["samples"][0]["reading"]["value"] += 1
    with pytest.raises(FactImportError, match=r"conflicting.*sample"):
        _import(
            _sources()[0],
            ImportSource("other.json", SourceFormat.ENVELOPE, json.dumps(other).encode()),
        )


def test_unknown_snapshot_and_corrupt_embedded_json_name_their_path() -> None:
    for snapshot in ("{", '{"max_rounds":2,"unknown":true}'):
        envelope = _envelope()
        envelope["strategy"]["live"]["flows"][0]["snapshot_json"] = snapshot
        with pytest.raises(FactImportError, match=r"damaged.json.*snapshot_json"):
            _import(
                ImportSource("damaged.json", SourceFormat.ENVELOPE, json.dumps(envelope).encode())
            )


def test_project_shell_reads_original_content_and_does_not_modify_the_store(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    project.state.create_project("example", now=now)
    manifest = project.state.new_run_manifest(
        "example",
        run_id="run",
        branch="vibesys-runs/run",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(
            id="dynamic",
            config_version=1,
            options={"max_rounds": 2, "judge_every": 2, "interface": "service"},
        ),
        trusted_input_baseline="0" * 40,
        now=now,
    )
    project.state.create_run(manifest)
    namespace = project.state.portable_namespace("run", "dynamic")
    legacy = (_FIXTURES / "legacy-v6-completed.json").read_bytes()
    namespace.write_bytes("state.json", legacy)
    project.state.portable_namespace("run", "runtime").write_bytes(
        "effective-objective.md", b"Original task."
    )
    before = project.state.portable_run_export("run").files
    (tmp_path / "OBJECTIVE.md").write_text("An edited task.")
    result = read_run_facts(project, "run", registry=FAKE_REGISTRY)
    assert any(content.data == b"Original task." for content in _payloads(result, "content"))
    assert all(content.data != b"An edited task." for content in _payloads(result, "content"))
    assert project.state.portable_run_export("run").files == before
    assert namespace.read_bytes("state.json") == legacy
