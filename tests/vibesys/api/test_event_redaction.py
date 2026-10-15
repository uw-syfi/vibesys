"""Credential redaction at the public semantic-event boundary."""

from datetime import UTC, datetime

import pytest

from vibesys.api import CoreEvent, CoreEventType, GateFinishedData, GateKind


def test_gate_output_redacts_credentials_before_event_publication() -> None:
    payload = GateFinishedData(
        gate=GateKind.ACCURACY,
        output_tail="API_TOKEN=gate-secret\nAuthorization: Bearer gate-bearer",
    )

    assert payload.output_tail == ("API_TOKEN=[REDACTED]\nAuthorization: Bearer [REDACTED]")


@pytest.mark.parametrize(
    ("stream", "content", "redacted"),
    [
        ("stdout", "API_KEY=stdout-secret", "API_KEY=[REDACTED]"),
        ("stderr", "request used Bearer stderr-secret", "request used Bearer [REDACTED]"),
    ],
)
def test_gate_subprocess_streams_redact_credentials_before_event_publication(
    stream: str, content: str, redacted: str
) -> None:
    event = CoreEvent.model_validate(
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "type": CoreEventType.SUBPROCESS_OUTPUT,
            "data": {
                "kind": "subprocess_output",
                "process_id": "accuracy-1",
                "process_kind": "accuracy_checker",
                "stream": stream,
                "content": content,
            },
        }
    )

    assert event.data is not None
    assert event.data.model_dump()["content"] == redacted


def test_workstream_failure_redacts_credentials_during_public_event_validation() -> None:
    event = CoreEvent.model_validate(
        {
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "type": CoreEventType.WORKSTREAM_FINISHED,
            "data": {
                "kind": "workstream_finished",
                "setup_failure": False,
                "refunded": False,
                "repeated_error": False,
                "last_error": {
                    "kind": "no-trusted-evidence",
                    "text": "password: workstream-secret; TOKEN=second-secret",
                },
            },
        }
    )

    assert event.data is not None
    assert event.data.model_dump()["last_error"]["text"] == (
        "password: [REDACTED]; TOKEN=[REDACTED]"
    )
