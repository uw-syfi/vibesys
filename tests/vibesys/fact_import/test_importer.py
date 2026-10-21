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
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.api.importing import (
    FactImportError,
    ImportSource,
    SourceFormat,
    import_run_facts,
    read_run_facts,
)
from vs_core.api import FlowState, RunEnvelope
from vs_core.api.testing import FAKE_REGISTRY
from vs_evaluation.api import (
    ContentDigest,
    EvidenceFingerprints,
    EvidenceMetric,
    EvidenceOutcome,
    TrustedEvidence,
)
from vs_evaluation.api import EvidenceKind as StageKind
from vs_evaluator_protocol.api import PartialMeasurement, Progress
from vs_project.api import (
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    StateDocumentDamagedError,
    StoredEnvelope,
)
from vs_project.api.testing import run_execution_record
from vs_proto.api import imported_facts as pb
from vs_proto.api import validate
from vs_runtime.api.core import EvidenceEntry, RuntimeRecord

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


def _receipt(family: str, part: str, data: dict[str, Any], key: str = "request-a") -> ImportSource:
    path = f"receipts/{family}/{sha256(key.encode()).hexdigest()}.{part}.json"
    return ImportSource(path, SourceFormat.RECEIPT, json.dumps(data).encode())


def test_receipts_preserve_unsealed_effects_and_reject_arbitrary_python_type_tags() -> None:
    begun = _receipt("executions", "execution", {"payload_digest": "digest", "phase": "begun"})
    imported = _import(begun)
    assert _payloads(imported, "external_identity")[0].kind == "unsealed-execution"
    done = _receipt(
        "executions",
        "execution",
        {
            "payload_digest": "digest",
            "phase": "done",
            "result_json": '{"sequence":1}',
            "result_type": None,
        },
    )
    _import(_sources()[0], done)
    for tag in ("arbitrary.Module", "os.system"):
        invalid = json.loads(done.data)
        invalid["result_type"] = tag
        source = _receipt("executions", "execution", invalid)
        with pytest.raises(FactImportError, match="result_type"):
            _import(source)


def test_settled_usage_keeps_unknowns_and_does_not_count_live_usage_updates() -> None:
    timestamp = "2026-01-01T00:00:00Z"
    rows = [
        {
            "type": "usage_update",
            "sequence": 1,
            "timestamp": timestamp,
            "data": {"kind": "usage_update", "input_tokens": 9999, "context_window": 100000},
        },
        {
            "type": "turn_usage_recorded",
            "sequence": 2,
            "timestamp": timestamp,
            "execution_id": "invocation-7",
            "data": {
                "kind": "turn_usage_recorded",
                "input_tokens": 1234,
                "output_tokens": 90,
                "total_cost_usd": 0.42,
                "duration_ms": 1500,
            },
        },
    ]
    source = ImportSource(
        "core-events.jsonl",
        SourceFormat.EVENTS,
        b"\n".join(json.dumps(row).encode() for row in rows) + b"\n",
    )
    result = _import(source)
    usage = {row.unit: row.quantity for row in _payloads(result, "usage")}
    assert usage["input_tokens"].value == "1234"
    assert usage["total_cost_usd"].value == 0.42
    assert usage["cache_read_input_tokens"].field == "unknown"
    assert len(usage) == 7


def test_guidance_retains_disposition_target_and_standing_mode() -> None:
    source = ImportSource(
        "control/inbox/advice.json",
        SourceFormat.CONTROL,
        b'{"control_id":"advice","action":"steer","text":"Focus on latency.","target":{"kind":"role","id":"planner"},"mode":"standing"}',
    )
    result = _import(source)
    guidance = _payloads(result, "guidance")[0]
    assert guidance.text == "Focus on latency."
    assert guidance.historical_target_kind.name == "ROLE"
    assert guidance.historical_target_id == "planner"
    assert guidance.mode.name == "STANDING"
    assert not guidance.delivered


@pytest.mark.parametrize("version", list(range(1, 10)))
def test_legacy_versions_keep_fact_semantics_without_changing_the_source_version(
    version: int,
) -> None:
    value = json.loads((_FIXTURES / "legacy-v6-completed.json").read_bytes())
    value["schema_version"] = version
    source = ImportSource(
        "dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(value).encode()
    )
    result = _import(source, strategy="dynamic")
    assert _payloads(result, "measurement")
    assert json.loads(source.data)["schema_version"] == version


@pytest.mark.parametrize(
    "fixture", sorted(Path("libs/vs-core/tests/envelopes").glob("v*.json")), ids=lambda p: p.stem
)
def test_every_retained_envelope_fixture_lowers_without_an_upgrade(fixture: Path) -> None:
    value = json.loads(fixture.read_bytes())
    for section in (value["strategy"]["live"], value["strategy"].get("base")):
        if section is not None:
            for flow in section["flows"]:
                if flow["name"] == "root":
                    flow["snapshot_json"] = '{"max_rounds":2}'
    source = ImportSource(fixture.name, SourceFormat.ENVELOPE, json.dumps(value).encode())
    result = _import(source)
    samples = [row.sample for row in _payloads(result, "measurement") if row.sample is not None]
    expected = {sample["run"]["root"] for sample in value["core"]["evaluation"].get("samples", [])}
    assert {sample.run.root for sample in samples} == expected
    assert json.loads(source.data)["schema_version"] == value["schema_version"]


def test_frozen_snapshot_schema_keeps_properties_named_title_and_the_original_provenance() -> None:
    value = _envelope()
    published = json.loads((_FIXTURES / "legacy-v6-completed.json").read_bytes())["search"]
    # This fixture exercises the permanent legacy search reader, including the plan's title.
    source = ImportSource(
        "single/state.json", SourceFormat.HYPOTHESES, json.dumps(published).encode()
    )
    result = _import(source)
    assert _payloads(result, "candidate")
    assert all(
        origin.sha256 == sha256(source.data).hexdigest()
        for fact in result.facts
        for origin in fact.imported_from
    )
    assert value["schema_version"] == 42


def test_unknown_envelope_version_and_cross_run_input_are_prerequisites() -> None:
    value = _envelope()
    value["schema_version"] = 999
    with pytest.raises(FactImportError, match="schema_version"):
        _import(ImportSource("future.json", SourceFormat.ENVELOPE, json.dumps(value).encode()))
    with pytest.raises(FactImportError, match="another run"):
        import_run_facts("another", [_sources()[0]], strategy="single", registry=FAKE_REGISTRY)


def test_accepted_stage_ledger_keeps_fingerprints_all_readings_and_partial_progress() -> None:

    digest = ContentDigest(value="1" * 64)
    evidence = TrustedEvidence(
        evidence_id="2" * 64,
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=StageKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=digest, evaluator=digest, workload=digest, environment=digest
        ),
        trusted_inputs=digest,
        outcome=EvidenceOutcome.FAILED,
        metrics=(EvidenceMetric(name="throughput", value=1.0, direction="max"),),
        partial_measurement=PartialMeasurement(
            name="warmup",
            value=2.0,
            direction="max",
            target=4.0,
            progress=Progress(completed=2, required=4, unit="rounds"),
        ),
        accepted_round=0,
    )
    entry = EvidenceEntry(evidence=evidence, purpose="official")
    result = _import(_receipt("evidence-ledger", "evidence", entry.model_dump(mode="json")))
    stage = _payloads(result, "measurement")[0].stage_evidence
    assert stage.candidate_digest == "1" * 64
    assert stage.environment_digest == "1" * 64
    assert stage.readings[0].value == 1.0
    assert stage.partial.reading.value == 2.0
    assert stage.partial.completed == "2"
    assert stage.outcome.name == "FAILED"
    assert stage.purpose.name == "OFFICIAL"


@given(count=st.integers(min_value=0, max_value=2**100))
def test_usage_counts_remain_exact_above_machine_integer_and_float_precision(count: int) -> None:
    value = _envelope()
    value["core"]["sessions"]["run_charges"] = [
        {"charge_id": {"kind": "charge", "root": "charge-a"}, "kind": "turn", "charged": count}
    ]
    result = _import(
        ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(value).encode())
    )
    usage = _payloads(result, "usage")[0]
    assert usage.quantity.field == "count"
    assert usage.quantity.value == str(count)


def test_current_guidance_preserves_the_exact_shared_target_and_content_reference() -> None:
    value = _envelope()
    artifact = {"artifact_id": {"kind": "artifact", "root": "note"}, "digest": "note-digest"}
    value["core"]["sessions"]["inputs"] = [
        {
            "input": {
                "input_id": {"kind": "input", "root": "advice"},
                "target": {"kind": "role", "role_id": {"kind": "role", "root": "planner"}},
                "artifact": artifact,
                "received_at": 1.0,
                "sequence": 1,
                "mode": "standing",
            }
        }
    ]
    result = _import(
        ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(value).encode())
    )
    guidance = _payloads(result, "guidance")[0]
    assert guidance.target.target.field == "role"
    assert guidance.artifact.digest == "note-digest"
    assert any(ref.digest == "note-digest" for ref in _payloads(result, "content_reference"))


@pytest.mark.parametrize(
    ("strategy", "config"),
    [
        ("dynamic", {"max_rounds": 2}),
        ("single-agent", {"max_rounds": 2}),
        ("multi-agent", {"max_rounds": 2}),
        ("plain", {"max_rounds": 2, "max_attempts_per_issue": 1, "max_issues_per_perf_eval": 1}),
        (
            "evolve",
            {
                "max_generations": 1,
                "children_per_generation": 2,
                "k_top_inspirations": 1,
                "k_random_inspirations": 0,
                "selection_temperature": 1.0,
                "frontier_bias": 0.5,
                "bootstrap_max_attempts": 2,
            },
        ),
    ],
)
def test_all_five_retained_root_snapshot_families_are_read_only(
    strategy: str, config: dict[str, Any]
) -> None:
    value = _envelope()["strategy"]
    value["live"]["flows"][0]["snapshot_json"] = json.dumps(config)
    source = ImportSource("flow.json", SourceFormat.FLOW, json.dumps(value).encode())
    result = _import(source, strategy=strategy)
    validate(result)
    assert json.loads(source.data)["live"]["flows"][0]["snapshot_json"] == json.dumps(config)


def test_current_store_and_legacy_fixtures_match_shared_fact_goldens() -> None:

    cases = {
        "current": _import(*_sources()),
        "legacy": _import(
            ImportSource(
                "dynamic/state.json",
                SourceFormat.LEGACY_DYNAMIC,
                (_FIXTURES / "legacy-v6-completed.json").read_bytes(),
            ),
            strategy="dynamic",
        ),
    }
    for name, result in cases.items():
        assert_exact_text(
            _FIXTURES / f"{name}.facts.json",
            json.dumps(json.loads(result.to_json()), sort_keys=True, indent=2) + "\n",
        )


def test_guidance_occurrences_coalesce_with_withdrawal_instead_of_redelivering_old_advice() -> None:
    advice = ImportSource(
        "control/inbox/advice.json",
        SourceFormat.CONTROL,
        b'{"control_id":"advice","action":"steer","text":"Focus on latency."}',
    )
    withdrawal = ImportSource(
        "control/inbox/withdraw.json",
        SourceFormat.CONTROL,
        b'{"control_id":"withdraw","action":"withdraw","withdraws":"advice"}',
    )
    result = _import(advice, withdrawal)
    guidance = _payloads(result, "guidance")
    assert len(guidance) == 1
    assert guidance[0].text == "Focus on latency."
    assert guidance[0].withdrawn
    assert guidance[0].disposed
    fact = next(
        fact for fact in result.facts if fact.value is not None and fact.value.field == "guidance"
    )
    assert {origin.path for origin in fact.imported_from} == {advice.path, withdrawal.path}


def test_parent_release_never_erases_a_childs_independent_ownership() -> None:
    value = _envelope()
    request = value["core"]["records"]["records"][0]["request_id"]
    scope = {"owner": {"kind": "run", "root": "run"}, "generation": 0}
    value["core"]["records"]["children"] = [
        {
            "resource_id": {"kind": "resource", "root": "child-live"},
            "scope": scope,
            "source_requests": [request],
        }
    ]
    result = _import(
        ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(value).encode())
    )
    assert any(
        identity.identity == "child-live" for identity in _payloads(result, "external_identity")
    )


def test_project_retained_journal_is_bounded_and_keeps_original_bytes(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    path = project.event_journal_path_for(project.root, "run")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"retained\n")
    assert project.retained_event_journal("run", max_bytes=100) == b"retained\n"
    with pytest.raises(StateDocumentDamagedError, match="byte limit"):
        project.retained_event_journal("run", max_bytes=1)
    assert path.read_bytes() == b"retained\n"


def test_registered_jobs_keep_physical_and_independent_child_identities() -> None:
    value = _envelope()
    value["core"]["evaluation"]["registered_jobs"] = [
        {
            "operation_id": {"kind": "operation", "root": "operation-a"},
            "request_id": {"kind": "request", "root": "request-a"},
            "scope": {"owner": {"kind": "run", "root": "run"}, "generation": 0},
            "resource_pool": {"kind": "pool", "root": "jobs"},
            "resource_id": {"kind": "resource", "root": "job-a"},
            "terminal": True,
            "children": [{"kind": "resource", "root": "child-a"}],
        }
    ]
    result = _import(
        ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(value).encode())
    )
    identities = {row.identity for row in _payloads(result, "external_identity")}
    assert identities >= {"operation-a", "job-a", "child-a"}
