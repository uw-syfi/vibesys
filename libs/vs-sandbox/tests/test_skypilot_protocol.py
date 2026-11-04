"""The bridge wire protocol: strict, versioned messages that reject version, operation and policy overrides."""

import pytest

from vs_sandbox.api.skypilot import (
    EvaluationRequest,
    FrameDecodeError,
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
    with pytest.raises(FrameDecodeError):
        decode_request(payload)


def test_a_request_carries_the_step_that_asked_for_it_and_refuses_an_empty_tag() -> None:
    request = EvaluationRequest(kind="accuracy", invocation_id="1" * 32, step_tag="step-a")

    assert decode_request(encode_message(request)).step_tag == "step-a"
    assert (
        decode_request(
            encode_message(EvaluationRequest(kind="accuracy", invocation_id="1" * 32))
        ).step_tag
        is None
    )
    with pytest.raises(FrameDecodeError):
        decode_request(
            b'{"version":2,"kind":"accuracy","invocation_id":"11111111111111111111111111111111","step_tag":""}\n'
        )
