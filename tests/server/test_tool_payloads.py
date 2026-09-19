"""Tests for the best-effort tool-result classifier."""

import pytest
from google.protobuf import json_format

from server.tool_payloads import classify_tool_result
from server.wire.v2 import events_pb2


class TestClassifyToolResult:
    def test_json_object_is_classified_with_parsed_value(self) -> None:
        payload = classify_tool_result('{"metric": "throughput", "value": 2400}')
        assert isinstance(payload, events_pb2.JsonResultPayload)
        assert json_format.MessageToDict(payload.value) == {"metric": "throughput", "value": 2400}

    def test_json_array_is_classified_despite_surrounding_whitespace(self) -> None:
        payload = classify_tool_result('  \n [1, {"a": true}, null] \n')
        assert isinstance(payload, events_pb2.JsonResultPayload)
        assert json_format.MessageToDict(payload.value) == [1, {"a": True}, None]

    @pytest.mark.parametrize("content", ["42", '"text"', "null", "true"])
    def test_scalar_json_is_not_classified(self, content: str) -> None:
        assert classify_tool_result(content) is None

    @pytest.mark.parametrize(
        "content",
        ['{"a": 1} trailing text', "[1, 2] and more", '{"a": 1}{"b": 2}'],
    )
    def test_trailing_garbage_is_not_classified(self, content: str) -> None:
        assert classify_tool_result(content) is None

    @pytest.mark.parametrize(
        "content",
        ["plain command output", "{not json", "[broken", "", "   \n\t "],
    )
    def test_plain_malformed_and_empty_text_is_not_classified(self, content: str) -> None:
        assert classify_tool_result(content) is None
