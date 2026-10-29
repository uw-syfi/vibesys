"""The byte-only Electron gateway contract is generated and validated in both languages."""

import json
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_proto.api import ValidationError, desktop_gateway, from_mapping, validate

_FIXTURES = Path(__file__).resolve().parents[3] / "proto/testdata/desktop/gateway.json"


@pytest.mark.parametrize("case", json.loads(_FIXTURES.read_text()), ids=lambda case: case["name"])
def test_gateway_cross_language_corpus(case: dict[str, object]) -> None:
    schema = (
        desktop_gateway.DesktopGatewayRequest
        if case["schema"] == "request"
        else desktop_gateway.DesktopGatewayReply
    )
    encoded = case["protobufHex"]
    assert isinstance(encoded, str)
    message = schema.from_binary(bytes.fromhex(encoded))
    if case["valid"]:
        validate(message)
        assert message.to_binary() == bytes.fromhex(encoded)
    else:
        with pytest.raises(ValidationError):
            validate(message)


@given(st.text(min_size=1).filter(lambda key: key not in {"kind", "host", "method", "bytes"}))
def test_gateway_rejects_unknown_routing_keys(key: str) -> None:
    with pytest.raises(ValueError, match="unknown keys"):
        from_mapping(desktop_gateway.DesktopGatewayRequest, {key: "unexpected"})
