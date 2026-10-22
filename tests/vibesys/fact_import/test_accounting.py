"""Imported usage publishes once as accounting evidence, or names why it cannot."""

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

from protobuf import Oneof
from protobuf.wkt.google.protobuf.any_pb import Any as AnyMessage
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vibesys.api.importing import (
    AccountingUnreconciled,
    ImportSource,
    SourceFormat,
    accounting_evidence,
    import_run_facts,
)
from vs_core.api.replay import AccountingAmount, AccountingLedger
from vs_core.api.testing import FAKE_REGISTRY
from vs_proto.api import Notice, NoticeCode, Record, Severity, replay

_FIXTURES = Path(__file__).with_name("fixtures")
Dimension = replay.AccountingDimension


def _events(*rows: dict[str, Any]) -> ImportSource:
    data = "\n".join(json.dumps(row) for row in rows).encode()
    return ImportSource("core-events.jsonl", SourceFormat.EVENTS, data)


def _usage(execution: str, tokens: int, cost: float) -> dict[str, Any]:
    return {
        "type": "turn_usage_recorded",
        "timestamp": "2026-01-01T00:00:00Z",
        "execution_id": execution,
        "data": {"kind": "turn_usage_recorded", "input_tokens": tokens, "total_cost_usd": cost},
    }


def _legacy(agent: dict[str, int]) -> ImportSource:
    legacy = json.loads((_FIXTURES / "legacy-v6-empty.json").read_bytes())
    legacy["agent"] = agent
    return ImportSource(
        "dynamic/state.json", SourceFormat.LEGACY_DYNAMIC, json.dumps(legacy).encode()
    )


def _publish(*sources: ImportSource, strategy: str = "single") -> replay.AccountingEvidence:
    facts = import_run_facts("run", sources, strategy=strategy, registry=FAKE_REGISTRY)
    evidence = accounting_evidence(facts, "import-a")
    assert isinstance(evidence, replay.AccountingEvidence)
    return evidence


def _notice(evidence: replay.AccountingEvidence) -> Record:
    detail = AnyMessage(type_url=evidence.desc().type_name, value=evidence.to_binary())
    return Record(
        seq=1,
        epoch=1,
        at=Timestamp(seconds=1),
        body=Oneof(
            "notice",
            Notice(
                severity=Severity.WARNING,
                code=NoticeCode.FRAMEWORK_WARNING,
                message="Imported accounting.",
                detail=detail,
            ),
        ),
    )


def _lifetime(*evidence: replay.AccountingEvidence) -> dict[Dimension, AccountingAmount]:
    return AccountingLedger.from_records(_notice(value) for value in evidence).totals()


def test_an_aggregate_beside_its_possible_components_is_unreconciled_not_guessed() -> None:
    facts = import_run_facts(
        "run",
        (_legacy({"input_tokens": 3, "output_tokens": 4}), _events(_usage("invocation-a", 3, 0.5))),
        strategy="dynamic",
        registry=FAKE_REGISTRY,
    )
    blocked = accounting_evidence(facts, "import-a")
    assert isinstance(blocked, AccountingUnreconciled)
    assert blocked.dimension == Dimension.INPUT_TOKENS
    assert {source.split(":")[0] for source in blocked.sources} == {
        "core-events.jsonl",
        "dynamic/state.json",
    }


def test_each_settled_turn_is_one_occurrence_and_reimport_charges_nothing_twice() -> None:
    evidence = _publish(_events(_usage("invocation-a", 3, 0.5), _usage("invocation-a", 4, 0.25)))
    identities = {
        observation.occurrence.id
        for observation in evidence.observations
        if observation.dimension == Dimension.INPUT_TOKENS and observation.occurrence is not None
    }
    assert identities == {"invocation-a@event:1", "invocation-a@event:2"}
    assert all(observation.origin_epoch == 0 for observation in evidence.observations)
    totals = _lifetime(evidence, evidence)
    assert totals[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(7))
    assert totals[Dimension.MONEY_USD] == AccountingAmount(Fraction(0.75))
    assert totals[Dimension.CACHE_READ_TOKENS] == AccountingAmount(unknown=True)


def test_unfinished_invocations_inside_an_aggregate_report_only_unknown_growth() -> None:
    started = {
        "type": "invocation_started",
        "timestamp": "2026-01-01T00:00:00Z",
        "execution_id": "invocation-b",
        "data": {"kind": "invocation_started", "system_prompt": "s", "user_prompt": "u"},
    }
    evidence = _publish(
        _legacy({"input_tokens": 9, "output_tokens": 4}), _events(started), strategy="dynamic"
    )
    (tokens,) = (item for item in evidence.coverage if item.dimension == Dimension.INPUT_TOKENS)
    assert [cell.occurrence.id for cell in tokens.cells if cell.occurrence] == ["invocation-b"]
    assert _lifetime(evidence)[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(9))
    late = replay.AccountingEvidence(
        observations=[
            replay.AccountingObservation(
                occurrence=replay.AccountingOccurrence(
                    kind=replay.AccountingOccurrenceKind.INVOCATION, id="invocation-b"
                ),
                dimension=Dimension.INPUT_TOKENS,
                revision=1,
                charged=replay.AccountingQuantity(value=Oneof("count", 5)),
            )
        ]
    )
    assert _lifetime(evidence, late)[Dimension.INPUT_TOKENS] == AccountingAmount(
        Fraction(9), unknown=True
    )


def test_charges_keep_their_own_identity_refunds_and_request_alias() -> None:
    envelope = json.loads((_FIXTURES / "envelope-v42.json").read_bytes())
    envelope["core"]["sessions"]["run_charges"] = [
        {
            "charge_id": {"kind": "charge", "root": "charge-a"},
            "kind": "attempt",
            "charged": 3,
            "refunded": 1,
            "source_request": {"kind": "request", "root": "request-a"},
        }
    ]
    source = ImportSource("envelope.json", SourceFormat.ENVELOPE, json.dumps(envelope).encode())
    evidence = _publish(source, _events(_usage("charge-a", 3, 0.5)))
    (charge,) = (item for item in evidence.observations if item.dimension == Dimension.ATTEMPTS)
    assert charge.occurrence is not None
    assert charge.occurrence.kind == replay.AccountingOccurrenceKind.CHARGE
    assert charge.aliases == ["request-a"]
    totals = _lifetime(evidence)
    assert totals[Dimension.ATTEMPTS] == AccountingAmount(Fraction(2))
    assert totals[Dimension.INPUT_TOKENS] == AccountingAmount(Fraction(3))
