"""Every language accepts the same valid host API corpus."""

import json
from pathlib import Path
from typing import NotRequired, TypedDict

import pytest

from vs_proto import api as p


class HostCase(TypedDict):
    name: str
    message: str
    value: dict[str, object]
    field: NotRequired[str]


_CORPUS = json.loads(
    (Path(__file__).resolve().parents[3] / "proto/testdata/host-validation.json").read_text()
)


@pytest.mark.parametrize(
    "case", [case for case in _CORPUS if "field" not in case], ids=lambda case: case["name"]
)
def test_host_validation_corpus(case: HostCase) -> None:
    message = getattr(p, case["message"]).from_json(json.dumps(case["value"]))
    p.validate(message)
    p.validate(type(message).from_binary(message.to_binary()))


def _literal(value: object) -> bool:
    if isinstance(value, list):
        return all(_literal(item) for item in value)
    if isinstance(value, dict):
        return all(not key.startswith("$") and _literal(item) for key, item in value.items())
    return True


@pytest.mark.parametrize(
    "case",
    [case for case in _CORPUS if "field" in case and _literal(case["value"])],
    ids=lambda case: case["name"],
)
def test_host_rejects_invalid_fields(case: HostCase) -> None:
    message = getattr(p, case["message"]).from_json(json.dumps(case["value"]))
    with pytest.raises(p.ValidationError) as failure:
        p.validate(message)
    details = "\n".join(
        str(violation.proto)
        + " "
        + ".".join(element.field_name for element in violation.proto.field.elements)
        for violation in failure.value.violations
        if violation.proto.field is not None
    )
    details += "\n".join(violation.proto.rule_id for violation in failure.value.violations)
    assert case["field"] in details
