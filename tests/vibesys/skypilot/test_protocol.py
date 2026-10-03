import pytest
from pydantic import ValidationError

from vs_sandbox.api.skypilot import (
    EvaluationRequest,
    OutputFrame,
    decode_request,
    decode_response,
    encode_message,
)


def test_protocol_round_trips_strict_versioned_messages() -> None:
    request = EvaluationRequest(kind="benchmark", invocation_id="1" * 32)
    frame = OutputFrame(type="stdout", data="measurement\n")

    assert decode_request(encode_message(request)) == request
    assert decode_response(encode_message(frame)) == frame


@pytest.mark.parametrize(
    "payload",
    [
        b'{"version":1,"kind":"accuracy","invocation_id":"11111111111111111111111111111111"}\n',
        b'{"version":2,"kind":"shell","invocation_id":"11111111111111111111111111111111"}\n',
        b'{"version":2,"kind":"accuracy","invocation_id":"11111111111111111111111111111111","host":"login"}\n',
    ],
)
def test_protocol_rejects_version_operation_and_policy_overrides(payload: bytes) -> None:
    with pytest.raises(ValidationError):
        decode_request(payload)
