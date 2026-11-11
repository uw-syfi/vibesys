"""The public observation-stream inventory is complete and structural."""

import pytest
from scripts.check_public_stream_contracts import Surface, repository_violations, violations


def test_repository_public_streams_are_classified() -> None:
    assert repository_violations() == []


_HOST = "proto/public.proto"
_TOPICS = "proto/topics.proto"
_SURFACE = (Surface(_HOST, "PublicService", "schema_hash"),)
_BASE_SOURCE = """
syntax = "proto3";
package example.v1;
service PublicService {
  rpc Observe(ObserveRequest) returns (stream ObserveResponse);
}
message ObserveResponse { oneof body { State snapshot = 1; Change change = 2; } }
"""


def _entry(**changes: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "example.v1.PublicService/Observe",
        "owner": _HOST,
        "delivery": "ordered",
        "initial": "snapshot_or_resume",
        "ordering": "log_sequence",
        "resume": "sequence",
        "cursor_scope": "durable_log",
        "completeness": "lossless",
        "bounds": "message",
        "liveness": "transport",
        "failure": "grpc_status",
        "compatibility": "schema_hash",
    }
    entry.update(changes)
    return entry


def _problems(source: str, entries: list[dict[str, object]]) -> list[str]:
    return violations({_HOST: source, _TOPICS: ""}, {"stream": entries}, _SURFACE, _TOPICS)


def test_new_public_stream_must_be_classified() -> None:
    source = _BASE_SOURCE.replace(
        "}", "  rpc NewStream(ObserveRequest) returns (stream ObserveResponse);\n}", 1
    )
    assert any(
        "NewStream is not classified" in problem for problem in _problems(source, [_entry()])
    )


@pytest.mark.parametrize(("field", "value"), [("delivery", "events"), ("failure", "text")])
def test_semantic_fields_are_closed(field: str, value: str) -> None:
    assert any(
        f"{field} must be one of" in problem
        for problem in _problems(_BASE_SOURCE, [_entry(**{field: value})])
    )


def test_delivery_family_rejects_an_incoherent_profile() -> None:
    entry = _entry(delivery="replacement")
    assert any(
        "replacement delivery requires initial" in problem
        for problem in _problems(_BASE_SOURCE, [entry])
    )


def test_composite_stream_classifies_every_response_arm() -> None:
    entry = _entry(
        delivery="composite",
        ordering="per_channel",
        cursor_scope="per_channel",
        completeness="per_channel",
        channel_oneof="body",
        channels={"snapshot": "bounded_snapshot"},
    )
    assert any("do not match body arms" in problem for problem in _problems(_BASE_SOURCE, [entry]))
