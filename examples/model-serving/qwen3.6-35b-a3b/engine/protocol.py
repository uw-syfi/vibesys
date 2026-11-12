"""OpenAI wire format: request parsing into typed values, and response bodies.

Pure: no model, tokenizer or socket. ``parse_completion`` and ``parse_chat``
validate a JSON body once and raise ``RequestError`` naming the offending
field; the ``*_chunk`` and ``*_response`` builders shape what the server sends.
Unknown vendor fields (``rid``, ``return_token_ids`` and the like) are accepted
and ignored, as OpenAI-compatible servers do, except the ones that would change
the result and that this baseline does not implement, which are refused.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

FinishReason = Literal["stop", "length"]

# Fields that change what a request returns and that this baseline does not implement.
_UNSUPPORTED = ("logprobs", "top_logprobs", "echo", "best_of", "suffix", "logit_bias")


class RequestError(ValueError):
    """A request the server rejects with HTTP ``status``."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Sampling:
    """How to pick tokens and when to stop."""

    max_tokens: int | None
    temperature: float = 1.0
    top_p: float = 1.0
    top_k: int = 0
    seed: int | None = None
    stop: tuple[str, ...] = ()
    ignore_eos: bool = False
    min_tokens: int = 0


@dataclass(frozen=True)
class CompletionRequest:
    """A parsed ``/v1/completions`` body. ``prompt`` is text or token ids."""

    prompt: str | tuple[int, ...]
    sampling: Sampling
    stream: bool
    include_usage: bool
    return_token_ids: bool


@dataclass(frozen=True)
class ChatRequest:
    """A parsed ``/v1/chat/completions`` body, ready for the chat template."""

    messages: tuple[dict[str, Any], ...]
    tools: tuple[dict[str, Any], ...] | None
    template_kwargs: dict[str, Any]
    sampling: Sampling
    stream: bool
    include_usage: bool
    return_token_ids: bool

    @property
    def thinking(self) -> bool:
        """Whether the template opens a reasoning block; off unless the request asks."""
        return self.template_kwargs.get("enable_thinking") is True


@dataclass
class Usage:
    """Token accounting for one request."""

    prompt_tokens: int
    cached_tokens: int
    completion_tokens: int = 0

    def body(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.prompt_tokens + self.completion_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
        }


@dataclass
class Envelope:
    """Identity shared by every chunk of one response."""

    model: str
    kind: Literal["text_completion", "chat.completion"]
    id: str = field(default_factory=lambda: f"cmpl-{uuid.uuid4().hex}")
    created: int = field(default_factory=lambda: int(time.time()))

    def base(self, suffix: str = "") -> dict[str, Any]:
        return {
            "id": self.id,
            "object": self.kind + suffix,
            "created": self.created,
            "model": self.model,
        }


def _require_body(body: object) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise RequestError("request body must be a JSON object")
    for key in _UNSUPPORTED:
        if body.get(key) not in (None, False, 0):
            raise RequestError(f"{key} is not supported by this server")
    if body.get("n", 1) != 1:
        raise RequestError("n must be 1")
    return body


def _number(body: dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    value = body.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int | float) or not low <= value <= high:
        raise RequestError(f"{key} must be a number in [{low}, {high}]")
    return float(value)


def _integer(body: dict[str, Any], key: str, *, low: int) -> int | None:
    value = body.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise RequestError(f"{key} must be an integer >= {low}")
    return value


def _flag(body: dict[str, Any], key: str) -> bool:
    value = body.get(key, False)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RequestError(f"{key} must be a boolean")
    return value


def _sampling(body: dict[str, Any], max_tokens: int | None) -> Sampling:
    stop = body.get("stop")
    if stop is None:
        stops: tuple[str, ...] = ()
    elif isinstance(stop, str):
        stops = (stop,)
    elif isinstance(stop, list) and all(isinstance(item, str) for item in stop):
        stops = tuple(stop)
    else:
        raise RequestError("stop must be a string or a list of strings")
    top_k = _integer(body, "top_k", low=-1)
    return Sampling(
        max_tokens=max_tokens,
        temperature=_number(body, "temperature", 1.0, 0.0, 2.0),
        top_p=_number(body, "top_p", 1.0, 0.0, 1.0) or 1.0,
        top_k=max(top_k or 0, 0),
        seed=_integer(body, "seed", low=0),
        stop=tuple(s for s in stops if s),
        ignore_eos=_flag(body, "ignore_eos"),
        min_tokens=_integer(body, "min_tokens", low=0) or 0,
    )


def _stream_options(body: dict[str, Any]) -> tuple[bool, bool]:
    stream = _flag(body, "stream")
    options = body.get("stream_options") or {}
    if not isinstance(options, dict):
        raise RequestError("stream_options must be an object")
    return stream, stream and options.get("include_usage") is True


def check_model(body: dict[str, Any], served: str) -> None:
    """Refuse a request naming a model this server does not serve."""
    model = body.get("model")
    if model is not None and model != served:
        raise RequestError(
            f"model {model!r} is not served here; this server serves {served!r}", 404
        )


def parse_completion(body: object) -> CompletionRequest:
    """Validate a ``/v1/completions`` body."""
    body = _require_body(body)
    prompt = body.get("prompt")
    if isinstance(prompt, list) and len(prompt) == 1 and isinstance(prompt[0], str | list):
        prompt = prompt[0]
    if isinstance(prompt, str):
        parsed: str | tuple[int, ...] = prompt
    elif isinstance(prompt, list) and all(
        isinstance(t, int) and not isinstance(t, bool) and t >= 0 for t in prompt
    ):
        parsed = tuple(prompt)
    else:
        raise RequestError("prompt must be a string or a list of token ids")
    stream, include_usage = _stream_options(body)
    max_tokens = _integer(body, "max_tokens", low=1)
    return CompletionRequest(
        prompt=parsed,
        sampling=_sampling(body, 16 if max_tokens is None else max_tokens),
        stream=stream,
        include_usage=include_usage,
        return_token_ids=_flag(body, "return_token_ids"),
    )


def _normalize_message(message: object, index: int) -> dict[str, Any]:
    if not isinstance(message, dict) or not isinstance(message.get("role"), str):
        raise RequestError(f"messages[{index}] must be an object with a string role")
    message = dict(message)
    calls = message.get("tool_calls")
    if calls:
        if not isinstance(calls, list):
            raise RequestError(f"messages[{index}].tool_calls must be a list")
        message["tool_calls"] = [_normalize_tool_call(call, index) for call in calls]
    return message


def _normalize_tool_call(call: object, index: int) -> dict[str, Any]:
    """Parse JSON-string arguments into the mapping the chat template iterates."""
    if not isinstance(call, dict):
        raise RequestError(f"messages[{index}].tool_calls entries must be objects")
    call = dict(call)
    function = call.get("function")
    if isinstance(function, dict) and isinstance(function.get("arguments"), str):
        try:
            arguments = json.loads(function["arguments"]) if function["arguments"].strip() else {}
        except json.JSONDecodeError as exc:
            raise RequestError(
                f"messages[{index}] has tool-call arguments that are not JSON"
            ) from exc
        call["function"] = {**function, "arguments": arguments}
    return call


def parse_chat(body: object) -> ChatRequest:
    """Validate a ``/v1/chat/completions`` body."""
    body = _require_body(body)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestError("messages must be a non-empty list")
    tools = body.get("tools")
    if tools is not None and not (
        isinstance(tools, list) and all(isinstance(t, dict) for t in tools)
    ):
        raise RequestError("tools must be a list of objects")
    template_kwargs = body.get("chat_template_kwargs") or {}
    if not isinstance(template_kwargs, dict):
        raise RequestError("chat_template_kwargs must be an object")
    max_tokens = _integer(body, "max_completion_tokens", low=1)
    if max_tokens is None:
        max_tokens = _integer(body, "max_tokens", low=1)
    stream, include_usage = _stream_options(body)
    return ChatRequest(
        messages=tuple(_normalize_message(m, i) for i, m in enumerate(messages)),
        tools=tuple(tools) if tools else None,
        template_kwargs=dict(template_kwargs),
        sampling=_sampling(body, max_tokens),
        stream=stream,
        include_usage=include_usage,
        return_token_ids=_flag(body, "return_token_ids"),
    )


def completion_chunk(
    env: Envelope, text: str, token_id: int | None, finish: FinishReason | None, with_ids: bool
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "text": text, "logprobs": None, "finish_reason": finish}
    if with_ids:
        choice["token_ids"] = [] if token_id is None else [token_id]
    return {**env.base(), "choices": [choice]}


def chat_chunk(
    env: Envelope,
    delta: dict[str, Any],
    token_id: int | None,
    finish: FinishReason | None,
    with_ids: bool,
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}
    if with_ids:
        choice["token_ids"] = [] if token_id is None else [token_id]
    return {**env.base(".chunk"), "choices": [choice]}


def usage_chunk(env: Envelope, usage: Usage) -> dict[str, Any]:
    suffix = ".chunk" if env.kind == "chat.completion" else ""
    return {**env.base(suffix), "choices": [], "usage": usage.body()}


def completion_response(
    env: Envelope,
    text: str,
    token_ids: list[int],
    finish: FinishReason,
    usage: Usage,
    with_ids: bool,
) -> dict[str, Any]:
    choice: dict[str, Any] = {"index": 0, "text": text, "logprobs": None, "finish_reason": finish}
    if with_ids:
        choice["token_ids"] = token_ids
    return {**env.base(), "choices": [choice], "usage": usage.body()}


def chat_response(
    env: Envelope,
    content: str,
    reasoning: str | None,
    token_ids: list[int],
    finish: FinishReason,
    usage: Usage,
    with_ids: bool,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    choice: dict[str, Any] = {
        "index": 0,
        "message": message,
        "logprobs": None,
        "finish_reason": finish,
    }
    if with_ids:
        choice["token_ids"] = token_ids
    return {**env.base(), "choices": [choice], "usage": usage.body()}


def error_body(message: str, status: int) -> dict[str, Any]:
    kind = "NotFoundError" if status == 404 else "BadRequestError"
    return {"error": {"message": message, "type": kind, "code": status}}
