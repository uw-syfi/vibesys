from __future__ import annotations

import itertools
import json

import httpx
import pytest

from vs_bench.transport import _extract_text, stream_sse


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


def _make_sse_response(chunks: list[str], status: int = 200) -> httpx.Response:
    lines = [f"data: {c}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    content = "".join(lines).encode()
    return httpx.Response(
        status_code=status,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(content),
    )


class TestStreamSSE:
    @pytest.mark.asyncio
    async def test_basic_streaming(self) -> None:
        chunks = [_chunk("Hello"), _chunk(" world"), _chunk("!")]
        response = _make_sse_response(chunks)

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.text == "Hello world!"
        assert result.token_count == 3
        assert result.error is None
        assert result.ttft is not None
        assert result.ttft >= 0
        assert len(result.itl) == 2

    @pytest.mark.asyncio
    async def test_empty_stream(self) -> None:
        response = _make_sse_response([])

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.text == ""
        assert result.token_count == 0
        assert result.ttft is None
        assert result.itl == []
        assert result.error is None

    @pytest.mark.asyncio
    async def test_finish_reason_captured(self) -> None:
        chunks = [_chunk("tok", finish_reason="stop")]
        response = _make_sse_response(chunks)

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.finish_reason == "stop"

    @pytest.mark.asyncio
    async def test_usage_captured(self) -> None:
        chunks = [_chunk("a"), _usage_chunk(5)]
        response = _make_sse_response(chunks)

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.usage is not None
        assert result.usage["completion_tokens"] == 5

    @pytest.mark.asyncio
    async def test_http_error(self) -> None:
        response = httpx.Response(status_code=500, text="Internal Server Error")
        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.error is not None
        assert result.token_count == 0

    @pytest.mark.asyncio
    async def test_latency_positive(self) -> None:
        ticks = itertools.count(0.0)
        chunks = [_chunk("x")]
        response = _make_sse_response(chunks)

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(
                client, "http://test/v1/completions", {"stream": True}, clock=ticks.__next__
            )

        # Read at the start, at the one token, and at the end: one second apart each.
        assert result.latency == 2.0
        assert result.ttft == 1.0

    @pytest.mark.asyncio
    async def test_single_token_no_itl(self) -> None:
        chunks = [_chunk("only")]
        response = _make_sse_response(chunks)

        transport = httpx.MockTransport(lambda _req: response)
        async with httpx.AsyncClient(transport=transport) as client:
            result = await stream_sse(client, "http://test/v1/completions", {"stream": True})

        assert result.token_count == 1
        assert result.itl == []
        assert result.ttft is not None
