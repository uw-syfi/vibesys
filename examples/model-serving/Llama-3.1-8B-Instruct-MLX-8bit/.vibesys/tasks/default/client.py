"""The streaming client: one /v1/completions request, timed, read to its end.

It knows nothing about schemas or what makes an answer right. The server's `finish_reason` is not
read, and a `Reply` has no field for it: the candidate is untrusted, so whether an answer was cut
off is for the judge to decide from the text (workload.judge), never for the server to say.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Reply:
    """What a request came to. A chunk is one server-sent text delta, about one token."""

    text: str
    chunks: int
    latency: float  # seconds, request sent to the last chunk (or to the failure)
    ttft: float | None  # seconds, request sent to the first chunk
    error: str | None


def stream_completion(
    client: httpx.Client, url: str, body: dict[str, Any], timeout: float
) -> Reply:
    """Send `body` to `url`/v1/completions and read the event stream. Never raises on a server fault."""
    pieces: list[str] = []
    ttft = None
    started = time.perf_counter()

    def failed(error: str) -> Reply:
        return Reply("", 0, time.perf_counter() - started, None, error)

    try:
        with client.stream("POST", f"{url}/v1/completions", json=body) as response:
            if response.status_code != httpx.codes.OK:
                response.read()
                return failed(f"HTTP {response.status_code}: {response.text[:200]}")
            if "text/event-stream" not in response.headers.get("content-type", ""):
                return failed("the response is not a text/event-stream")
            for line in response.iter_lines():
                if time.perf_counter() - started > timeout:
                    return failed(f"no end within {timeout:.0f} s")
                if not line.startswith("data: "):
                    continue
                data = line.removeprefix("data: ").strip()
                if data == "[DONE]":
                    break
                text = delta(json.loads(data))
                if text:
                    ttft = ttft if ttft is not None else time.perf_counter() - started
                    pieces.append(text)
    except (httpx.HTTPError, ValueError) as error:
        return failed(f"{type(error).__name__}: {error}")
    return Reply("".join(pieces), len(pieces), time.perf_counter() - started, ttft, None)


def delta(event: Any) -> str:
    """The text an OpenAI-style completion chunk carries ("" when it carries none).

    Raises:
        ValueError: The event is not a JSON object.
    """
    if not isinstance(event, dict):
        msg = "an event is not a JSON object"
        raise ValueError(msg)
    choices = event.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return ""
    text = choices[0].get("text")
    return text if isinstance(text, str) else ""
