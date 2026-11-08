from __future__ import annotations

import itertools
import json
from typing import TYPE_CHECKING

import httpx
import pytest

from vs_bench.transport import StreamResult, _extract_text, stream_sse

if TYPE_CHECKING:
    from collections.abc import Callable

_URL = "http://test/v1/completions"


def _chunk(text: str, finish_reason: str | None = None) -> str:
    choice: dict[str, object] = {"text": text}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason
    return json.dumps({"choices": [choice]})


def _usage_chunk(completion_tokens: int) -> str:
    return json.dumps(
        {
            "choices": [{"text": ""}],
            "usage": {"prompt_tokens": 10, "completion_tokens": completion_tokens},
        }
    )


class TestExtractText:
    def test_completions_format(self) -> None:
        assert _extract_text({"choices": [{"text": "hello"}]}) == "hello"

    def test_delta_content(self) -> None:
        assert _extract_text({"choices": [{"delta": {"content": "hi"}}]}) == "hi"

    def test_empty_choices(self) -> None:
        assert _extract_text({"choices": []}) == ""

    def test_no_choices(self) -> None:
        assert _extract_text({}) == ""

    def test_empty_text(self) -> None:
        assert _extract_text({"choices": [{"text": ""}]}) == ""


def _sse_response(lines: list[str], status: int = 200) -> httpx.Response:
    """A streaming response whose body is the given raw SSE lines, verbatim."""
    content = "".join(f"{line}\n\n" for line in lines).encode()
    return httpx.Response(
        status_code=status,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(content),
    )


def _make_sse_response(chunks: list[str], status: int = 200) -> httpx.Response:
    return _sse_response([f"data: {c}" for c in chunks] + ["data: [DONE]"], status)


async def _run(
    response: httpx.Response, *, clock: Callable[[], float] | None = None
) -> StreamResult:
    """Drive stream_sse against an in-memory response. One shared client keeps the
    test's real-API surface (tests/quality/real_api_baseline.jsonl) to a single site."""
    transport = httpx.MockTransport(lambda _req: response)
    async with httpx.AsyncClient(transport=transport) as client:
        if clock is None:
            return await stream_sse(client, _URL, {"stream": True})
        return await stream_sse(client, _URL, {"stream": True}, clock=clock)


class TestStreamSSE:
    @pytest.mark.asyncio
    async def test_basic_streaming(self) -> None:
        result = await _run(_make_sse_response([_chunk("Hello"), _chunk(" world"), _chunk("!")]))

        assert result.text == "Hello world!"
        assert result.token_count == 3
        assert result.error is None
        assert result.ttft is not None
        assert result.ttft >= 0
        assert len(result.itl) == 2

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        result = await _run(_make_sse_response([]))

        assert result.text == ""
        assert result.token_count == 0
        assert result.ttft is None
        assert result.itl == []
        assert result.error is None

    @pytest.mark.asyncio
    async def test_blank_data_line_is_a_keepalive_not_a_parse_error(self) -> None:
        # Some proxies interleave a blank "data: " line between real chunks as an
        # idle-timeout keep-alive; it carries no JSON and must not end the stream.
        response = _sse_response(
            [f"data: {_chunk('Hello')}", "data: ", f"data: {_chunk(' world')}", "data: [DONE]"]
        )

        result = await _run(response)

        assert result.text == "Hello world"
        assert result.token_count == 2
        assert result.error is None

    @pytest.mark.asyncio
    async def test_finish_reason_captured(self) -> None:
        result = await _run(_make_sse_response([_chunk("tok", finish_reason="stop")]))

        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_usage_captured(self) -> None:
        result = await _run(_make_sse_response([_chunk("a"), _usage_chunk(5)]))

        assert result.usage is not None
        assert result.usage["completion_tokens"] == 5

    @pytest.mark.asyncio
    async def test_http_error(self) -> None:
        result = await _run(httpx.Response(status_code=500, text="Internal Server Error"))

        assert result.error is not None
        assert result.token_count == 0

    @pytest.mark.asyncio
    async def test_latency_positive(self) -> None:
        ticks = itertools.count(0.0)

        result = await _run(_make_sse_response([_chunk("x")]), clock=ticks.__next__)

        # Read at the start, at the one token, and at the end: one second apart each.
        assert result.latency == 2.0
        assert result.ttft == 1.0

    @pytest.mark.asyncio
    async def test_single_token_no_itl(self) -> None:
        result = await _run(_make_sse_response([_chunk("only")]))

        assert result.token_count == 1
        assert result.itl == []
        assert result.ttft is not None
