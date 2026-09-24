"""HTTP transport helpers with protocol-intrinsic timing."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


@dataclass
class StreamResult:
    """Result of a streaming SSE request with token-level timing."""

    text: str
    token_count: int
    latency: float
    ttft: float | None
    itl: list[float] = field(default_factory=list)
    error: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int] | None = None


def _extract_text(chunk: dict[str, object]) -> str:
    """Extract generated text from an SSE chunk (OpenAI completions format)."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    choice = choices[0]
    if not isinstance(choice, dict):
        return ""
    text = choice.get("text")
    if isinstance(text, str) and text:
        return text
    delta = choice.get("delta")
    if isinstance(delta, dict):
        content = delta.get("content") or delta.get("text") or ""
        if isinstance(content, str):
            return content
    return ""


def _extract_finish_reason(chunk: dict[str, object]) -> str | None:
    """Extract finish_reason from an SSE chunk if present."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, dict):
        return None
    reason = choice.get("finish_reason")
    return reason if isinstance(reason, str) else None


def _extract_usage(chunk: dict[str, object]) -> dict[str, int] | None:
    """Extract usage dict from an SSE chunk if present."""
    chunk_usage = chunk.get("usage")
    if not isinstance(chunk_usage, dict):
        return None
    if not chunk_usage.get("completion_tokens"):
        return None
    return {k: v for k, v in chunk_usage.items() if isinstance(v, int)}


async def stream_sse(
    client: httpx.AsyncClient,
    url: str,
    body: dict[str, object],
    *,
    timeout: float = 180.0,
) -> StreamResult:
    """POST a streaming request and parse SSE lines with token-level timing.

    Measures TTFT (time to first token) and inter-token latencies. The request
    body is sent as-is; ``stream: true`` should already be included by the
    caller if desired (this function does not inject it).
    """
    started = time.perf_counter()
    first_token: float | None = None
    token_timestamps: list[float] = []
    token_count = 0
    text_parts: list[str] = []
    finish_reason: str | None = None
    usage: dict[str, int] | None = None

    try:
        async with client.stream("POST", url, json=body, timeout=timeout) as resp:
            resp.raise_for_status()
            async for raw_line in resp.aiter_lines():
                if not raw_line.startswith("data: "):
                    continue
                payload = raw_line[len("data: ") :].strip()
                if payload == "[DONE]":
                    break
                chunk: dict[str, object] = json.loads(payload)
                text = _extract_text(chunk)
                if text:
                    now = time.perf_counter()
                    text_parts.append(text)
                    token_count += 1
                    token_timestamps.append(now)
                    if first_token is None:
                        first_token = now
                reason = _extract_finish_reason(chunk)
                if reason is not None:
                    finish_reason = reason
                u = _extract_usage(chunk)
                if u is not None:
                    usage = u
    except Exception as exc:
        return StreamResult(
            text="".join(text_parts),
            token_count=token_count,
            latency=time.perf_counter() - started,
            ttft=None if first_token is None else first_token - started,
            error=str(exc),
        )

    done = time.perf_counter()
    itl = [token_timestamps[i] - token_timestamps[i - 1] for i in range(1, len(token_timestamps))]
    return StreamResult(
        text="".join(text_parts),
        token_count=token_count,
        latency=done - started,
        ttft=None if first_token is None else first_token - started,
        itl=itl,
        finish_reason=finish_reason,
        usage=usage,
    )
