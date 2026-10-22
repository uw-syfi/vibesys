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
    RetainedReadLimits,
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


def test_baseline_content_digest_is_not_fabricated_into_an_input_fingerprint() -> None:
    inputs = _payloads(_import(_sources()[0]), "input")
    assert inputs
    assert all(item.baseline.digest == "base-digest" for item in inputs)
    assert all(not item.input_fingerprint for item in inputs)


@pytest.mark.parametrize(
    ("key", "replacement"),
    [("next_publication_sequence", 999), ("delivery_cursor", 999)],
)
def test_runtime_cross_field_corruption_is_a_named_prerequisite(key: str, replacement: int) -> None:
    value = json.loads(_sources()[2].data)
    value[key] = replacement
    with pytest.raises(FactImportError, match=r"broken-runtime.json"):
        _import(
            ImportSource("broken-runtime.json", SourceFormat.RUNTIME, json.dumps(value).encode())
        )


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


_JSON_VALUES = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=20), children, max_size=4)
    ),
    max_leaves=12,
)


@given(value=_JSON_VALUES, source_format=st.sampled_from(tuple(SourceFormat)))
def test_generated_json_stores_are_total(value: object, source_format: SourceFormat) -> None:
    source = ImportSource("generated.json", source_format, json.dumps(value).encode())
    try:
        outcome = _import(source)
    except FactImportError as error:
        outcome = error
    if isinstance(outcome, FactImportError):
        assert outcome.path
    else:
        validate(outcome)
        assert outcome.to_binary() == _import(source).to_binary()


@pytest.mark.parametrize("data", [b"null", b"1", b"true"])
def test_nonobject_round_records_name_the_source_instead_of_escaping_the_decoder(
    data: bytes,
) -> None:
    with pytest.raises(FactImportError, match=r"round.json"):
        _import(ImportSource("round.json", SourceFormat.ROUND, data))


def test_pretrace_round_spellings_work_inside_retained_hypothesis_snapshots() -> None:
    state = json.loads((_FIXTURES / "legacy-v6-completed.json").read_bytes())["search"]
    round_value = state["hypotheses"][0]["rounds"][0]
    round_value["round"] = round_value.pop("round_number")
    round_value["implementer_driver"] = "agentshim"
    round_source = ImportSource("round.json", SourceFormat.ROUND, json.dumps(round_value).encode())
    snapshot_source = ImportSource(
        "single/state.json", SourceFormat.HYPOTHESES, json.dumps(state).encode()
    )
    expected = _payloads(_import(round_source), "measurement")
    assert expected
    assert expected[0] in _payloads(_import(snapshot_source), "measurement")
    round_value["implementer_driver"] = "unknown-driver"
    with pytest.raises(FactImportError, match="implementer_driver"):
        _import(ImportSource("round.json", SourceFormat.ROUND, json.dumps(round_value).encode()))


@pytest.mark.parametrize("key", ["hypothesis:h1", "unknown:h1", "role:judge"])
def test_retained_sessions_keep_external_identity_and_validate_the_stored_key(key: str) -> None:
    source = ImportSource(
        "agent/sessions.json",
        SourceFormat.SESSIONS,
        json.dumps(
            {
                "schema_version": 1,
                "sessions": {
                    key: {
                        "spec_fingerprint": "fingerprint",
                        "session_id": "session-a",
                        "provider": "codex",
                    }
                },
            }
        ).encode(),
    )
    if key == "hypothesis:h1":
        assert _payloads(_import(source), "external_identity")[0].identity == "session-a"
    else:
        with pytest.raises(FactImportError, match="sessions"):
            _import(source)


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


def _project_run(root: Path) -> Project:
    project = Project.open(root)
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
    return project


def test_project_shell_reads_original_content_and_does_not_modify_the_store(tmp_path: Path) -> None:
    project = _project_run(tmp_path)
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


_LIMITS = RetainedReadLimits(document_bytes=64 * 1024, run_bytes=128 * 1024)


@pytest.mark.parametrize(
    ("files", "failure"),
    [
        # A namespace no format imports is ignored unread, at any size.
        ({("logs", "profile.bin"): 10 * _LIMITS.run_bytes}, None),
        ({("artifacts", "trace.bin"): _LIMITS.document_bytes}, None),
        ({("artifacts", "trace.bin"): _LIMITS.document_bytes + 1}, "artifacts/trace.bin"),
        (
            {("artifacts", f"part-{index}.bin"): _LIMITS.document_bytes for index in range(2)},
            "<run>",
        ),
    ],
)
def test_project_shell_reads_only_imported_files_within_their_byte_limits(
    tmp_path: Path, files: dict[tuple[str, str], int], failure: str | None
) -> None:
    project = _project_run(tmp_path)
    for (namespace, name), size in files.items():
        project.state.portable_namespace("run", namespace).write_bytes(name, b"x" * size)
    if failure is None:
        result = read_run_facts(project, "run", registry=FAKE_REGISTRY, limits=_LIMITS)
        sizes = {len(content.data) for content in _payloads(result, "content")}
        assert sizes <= {size for (namespace, _), size in files.items() if namespace != "logs"}
    else:
        with pytest.raises(FactImportError) as error:
            read_run_facts(project, "run", registry=FAKE_REGISTRY, limits=_LIMITS)
        assert error.value.path == failure


def test_project_shell_bounds_the_retained_journal_by_the_document_limit(tmp_path: Path) -> None:
    project = _project_run(tmp_path)
    path = project.event_journal_path_for(project.root, "run")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (_LIMITS.document_bytes + 1))
    with pytest.raises(FactImportError) as error:
        read_run_facts(project, "run", registry=FAKE_REGISTRY, limits=_LIMITS)
    assert error.value.path == "core-events.jsonl"


@pytest.mark.parametrize(
    ("document", "run"), [(0, 1), (2, 1), (RetainedReadLimits().document_bytes + 1, 2**40)]
)
def test_read_limits_reject_a_document_limit_outside_the_run_and_source_limits(
    document: int, run: int
) -> None:
    with pytest.raises(ValueError, match="document_bytes"):
        RetainedReadLimits(document_bytes=document, run_bytes=run)


@pytest.mark.parametrize(("version", "strategy"), [(3, "multi-agent"), (4, "single-agent")])
def test_retired_manifests_keep_original_config_without_a_snapshot_upgrade(
    tmp_path: Path, version: int, strategy: str
) -> None:
    data = (_FIXTURES / f"manifest-v{version}.json").read_bytes()
    source = ImportSource("run.json", SourceFormat.MANIFEST, data)
    original = _payloads(_import(source, strategy=strategy), "input")[0]
    assert original.config[0].data == data
    assert original.input_fingerprint == "1" * 64
    project = Project.open(tmp_path)
    # A boundary fixture recreates the retained layout, including an unsupported manifest.
    path = tmp_path / ".vibesys" / "state" / "runs" / "run" / "run.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    result = read_run_facts(project, "run", registry=FAKE_REGISTRY)
    assert _payloads(result, "input") == [original]
    assert path.read_bytes() == data


@pytest.mark.parametrize("created_at", ["yesterday", "2026-01-01T00:00:00"])
def test_retired_manifest_dates_keep_the_original_aware_datetime_contract(created_at: str) -> None:
    value = json.loads((_FIXTURES / "manifest-v3.json").read_bytes())
    value["created_at"] = created_at
    with pytest.raises(FactImportError, match="created_at"):
        _import(
            ImportSource("run.json", SourceFormat.MANIFEST, json.dumps(value).encode()),
            strategy="multi-agent",
        )


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


@given(
    turns=st.lists(
        st.tuples(st.sampled_from(["invocation-a", "invocation-b"]), st.integers(0, 10**9)),
        min_size=1,
        max_size=8,
    ),
    sequenced=st.booleans(),
)
def test_every_settled_turn_of_a_reinvoked_invocation_is_conserved(
    turns: list[tuple[str, int]], *, sequenced: bool
) -> None:
    # A provider-session change re-invokes one invocation id; each turn settles its own usage.
    rows = [
        {
            "type": "turn_usage_recorded",
            "timestamp": "2026-01-01T00:00:00Z",
            "execution_id": invocation,
            "data": {"kind": "turn_usage_recorded", "input_tokens": amount, "total_cost_usd": 0.5},
            **({"sequence": index} if sequenced else {}),
        }
        for index, (invocation, amount) in enumerate(turns, start=1)
    ]
    source = ImportSource(
        "core-events.jsonl",
        SourceFormat.EVENTS,
        b"\n".join(json.dumps(row).encode() for row in rows),
    )
    usage = _payloads(_import(source), "usage")
    tokens = [int(row.quantity.value) for row in usage if row.unit == "input_tokens"]
    costs = [row.quantity.value for row in usage if row.unit == "total_cost_usd"]
    assert sorted(tokens) == sorted(amount for _, amount in turns)
    assert costs == [0.5] * len(turns)
    assert {row.scope.name for row in usage} == {"INVOCATION"}


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


def test_aggregate_and_component_usage_remain_distinct_overlapping_observations() -> None:
    legacy = json.loads((_FIXTURES / "legacy-v6-empty.json").read_bytes())
    legacy["agent"] = {"input_tokens": 3, "output_tokens": 4}
    event = {
        "type": "turn_usage_recorded",
        "timestamp": "2026-01-01T00:00:00Z",
        "execution_id": "invocation-a",
        "data": {"kind": "turn_usage_recorded", "input_tokens": 3},
    }
    result = _import(
        ImportSource(
            "dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(legacy).encode()
        ),
        ImportSource("core-events.jsonl", SourceFormat.EVENTS, json.dumps(event).encode()),
        strategy="dynamic",
    )
    usage = [row for row in _payloads(result, "usage") if row.unit == "input_tokens"]
    assert {row.scope.name for row in usage} == {"AGGREGATE", "INVOCATION"}
    assert len(usage) == 2
    assert all(row.quantity.value == "3" for row in usage)


def test_charge_and_invocation_identifiers_have_separate_accounting_domains() -> None:
    envelope = _envelope()
    envelope["core"]["sessions"]["run_charges"] = [
        {
            "charge_id": {"kind": "charge", "root": "invocation-a:input_tokens"},
            "kind": "turn",
            "charged": 2,
        }
    ]
    event = {
        "type": "turn_usage_recorded",
        "timestamp": "2026-01-01T00:00:00Z",
        "execution_id": "invocation-a",
        "data": {"kind": "turn_usage_recorded", "input_tokens": 3},
    }
    result = _import(
        ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(envelope).encode()),
        ImportSource("core-events.jsonl", SourceFormat.EVENTS, json.dumps(event).encode()),
    )
    usage = _payloads(result, "usage")
    assert any(row.scope.name == "CHARGE" and row.quantity.value == "2" for row in usage)
    assert any(row.scope.name == "INVOCATION" and row.quantity.value == "3" for row in usage)


# Each fixture was written by the dynamic loop's own models at the last commit of its
# version (v1 1ff29600b ... v9 6499f2333); legacy-v1-oldest-writer.json by the first one,
# 0bdcbefe0. One evaluated workstream measured two metrics; another failed after retries.
_LEGACY_WRITERS = sorted(_FIXTURES.glob("legacy-v*-writer.json"))


@pytest.mark.parametrize("fixture", _LEGACY_WRITERS, ids=lambda path: path.stem)
def test_every_legacy_dynamic_version_lowers_the_facts_its_own_release_wrote(
    fixture: Path,
) -> None:
    source = ImportSource("dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, fixture.read_bytes())
    result = _import(source, strategy="dynamic")
    readings = {
        (row.historical_commit, row.historical_reading.metric, row.historical_reading.value)
        for row in _payloads(result, "measurement")
    }
    assert readings == {
        ("candidate-1-revision-3", "throughput", 2.0),
        ("candidate-1-revision-3", "latency_ms", 7.5),
    }
    assert {row.git_commit for row in _payloads(result, "candidate")} == {
        "fake-revision-1",
        "candidate-1-revision-3",
    }
    budget = {row.identity: row.quantity.value for row in _payloads(result, "usage")}
    assert budget == {"workstream:1": "1", "workstream:2": "3"}


def test_legacy_writers_cover_the_oldest_and_every_later_version() -> None:
    versions = {json.loads(path.read_bytes())["schema_version"] for path in _LEGACY_WRITERS}
    assert versions == set(range(1, 10))


@given(
    fixture=st.sampled_from(["legacy-v1-oldest-writer.json", "legacy-v4-writer.json"]),
    spent=st.integers(min_value=0, max_value=2**40),
    refunded=st.integers(min_value=0, max_value=50),
)
def test_legacy_retry_budget_is_a_budget_marker_and_never_an_attempt_count(
    fixture: str, spent: int, refunded: int
) -> None:
    value = json.loads((_FIXTURES / fixture).read_bytes())
    workstream = value["workstreams"][1]
    if "budget" in workstream:
        workstream["budget"] = {"spent": spent, "refunded": refunded}
    else:
        workstream["attempts"] = spent
    source = ImportSource(
        "dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(value).encode()
    )
    usage = {row.identity: row for row in _payloads(_import(source, strategy="dynamic"), "usage")}
    marker = usage["workstream:2"]
    assert all(row.unit == "retry_budget_spent" for row in usage.values())
    assert marker.quantity.value == str(spent)
    assert marker.refunded_count == str(refunded if "budget" in workstream else 0)


@pytest.mark.parametrize(
    "fixture", sorted(Path("libs/vs-core/tests/envelopes").glob("v*.json")), ids=lambda p: p.stem
)
def test_every_retained_envelope_fixture_lowers_without_an_upgrade(fixture: Path) -> None:
    value = json.loads(fixture.read_bytes())
    value["strategy_id"]["root"] = "single"
    value["core"]["run"]["declaration"]["strategy_id"]["root"] = "single"
    value["state_schema"]["name"] = "single.flow-state"
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
    with pytest.raises(FactImportError, match="another strategy"):
        _import(_sources()[0], strategy="dynamic")


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
    assert usage.scope.name == "CHARGE"


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


def test_snapshot_only_run_keeps_all_accepted_metrics_and_partial_progress() -> None:
    source = ImportSource(
        "flow.json", SourceFormat.FLOW, (_FIXTURES / "flow-multimetric.json").read_bytes()
    )
    result = _import(source, strategy="dynamic")
    accepted = [
        row.accepted_reading
        for row in _payloads(result, "measurement")
        if row.accepted_reading is not None
    ]
    assert len(accepted) == 2
    benchmark = next(row for row in accepted if row.key.evidence_id.root == "benchmark")
    assert benchmark.key.source_request.root == "benchmark-request"
    assert [(reading.metric, reading.value) for reading in benchmark.readings] == [
        ("throughput", 2.0),
        ("p99", 3.0),
    ]
    assert benchmark.partial.completed == "1"
    assert benchmark.partial.required == "4"
    assert not benchmark.passed
    assert all(
        origin.sha256 == sha256(source.data).hexdigest()
        for fact in result.facts
        for origin in fact.imported_from
    )
    assert_exact_text(
        _FIXTURES / "flow-multimetric.facts.json",
        json.dumps(json.loads(result.to_json()), sort_keys=True, indent=2) + "\n",
    )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_snapshot_accepted_metrics_reject_nonfinite_values_with_the_embedded_path(
    value: float,
) -> None:
    flow = json.loads((_FIXTURES / "flow-multimetric.json").read_bytes())
    published = json.loads(flow["live"]["published"])
    published["parents"][0]["benchmark"]["metrics"][0]["value"] = value
    flow["live"]["published"] = json.dumps(published)
    with pytest.raises(FactImportError, match=r"flow.json.*published.*metrics.*value"):
        _import(
            ImportSource("flow.json", SourceFormat.FLOW, json.dumps(flow).encode()),
            strategy="dynamic",
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


def test_legacy_guidance_identity_and_mode_survive_note_reordering() -> None:
    value = json.loads((_FIXTURES / "legacy-v6-empty.json").read_bytes())
    notes = [
        {"note_sha256": name * 64, "text": name, "sent_at_s": 1.0, "interrupt": True}
        for name in ("a", "b")
    ]
    value["agent"] = {"steers": {"W1": notes}}
    first = _import(
        ImportSource("dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(value).encode()),
        strategy="dynamic",
    )
    notes.reverse()
    second = _import(
        ImportSource("dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(value).encode()),
        strategy="dynamic",
    )
    assert _payloads(first, "guidance") == _payloads(second, "guidance")
    assert all(row.mode.name == "INTERRUPT" for row in _payloads(first, "guidance"))


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


def test_creator_request_release_does_not_erase_a_retained_resource_binding() -> None:
    value = _envelope()
    receipt = _receipt(
        "workspace-bindings",
        "binding",
        {
            "ensure_request": value["core"]["records"]["records"][0]["request_id"],
            "mode": "isolated-child",
            "base": value["core"]["run"]["facts"]["baseline"],
            "resource_id": {"kind": "resource", "root": "retained-workspace"},
            "workspace_id": "workspace-a",
        },
    )
    result = _import(_sources()[0], receipt)
    assert any(
        row.identity == "retained-workspace" for row in _payloads(result, "external_identity")
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
