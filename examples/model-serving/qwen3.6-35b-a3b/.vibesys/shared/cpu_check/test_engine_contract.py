"""The baseline engine's HTTP contract, checked on a tiny random checkpoint.

These are the behaviors the tasks' benchmarks and service checker depend on:
exact output lengths under ``ignore_eos``, accurate prompt, cached-prompt and
output usage, one SSE event per generated token, repeatable greedy output, and
prefix-cache reuse that never changes what is generated.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from engine import experts
from engine.generator import experts_implementation

from conftest import CHUNK, Server

_TIMEOUT = 120


def _stream(server: Server, route: str, body: dict[str, Any]) -> list[dict[str, Any]]:
    events = []
    with httpx.stream(
        "POST", f"{server.url}{route}", json={**body, "stream": True}, timeout=_TIMEOUT
    ) as response:
        response.raise_for_status()
        assert response.headers["content-type"] == "text/event-stream"
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    for line in lines[:-1]:
        events.append(json.loads(line.removeprefix("data: ")))
    return events


def _completion(
    server: Server, prompt: list[int], max_tokens: int, **extra: Any
) -> tuple[list[dict], dict]:
    """Stream like Request Factory's session runner: token ids in, token ids and usage out."""
    body = {
        "model": server.model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": 0,
        "ignore_eos": True,
        "return_token_ids": True,
        "stream_options": {"include_usage": True},
        **extra,
    }
    events = _stream(server, "/v1/completions", body)
    *chunks, last = events
    assert last["choices"] == []
    return chunks, last["usage"]


def _ids(chunks: list[dict]) -> list[int]:
    return [t for c in chunks for t in c["choices"][0]["token_ids"]]


def test_health_and_models(server: Server) -> None:
    assert httpx.get(f"{server.url}/health").status_code == 200
    models = httpx.get(f"{server.url}/v1/models").json()
    assert [m["id"] for m in models["data"]] == [server.model]


@pytest.mark.parametrize("max_tokens", [1, 2, 7, 33])
def test_ignore_eos_streams_exactly_the_requested_tokens(server: Server, max_tokens: int) -> None:
    prompt = list(range(40, 40 + 11 + max_tokens))
    chunks, usage = _completion(server, prompt, max_tokens)
    assert len(chunks) == max_tokens
    assert all(len(c["choices"][0]["token_ids"]) == 1 for c in chunks)
    assert [c["choices"][0]["finish_reason"] for c in chunks] == [None] * (max_tokens - 1) + [
        "length"
    ]
    assert usage["prompt_tokens"] == len(prompt)
    assert usage["completion_tokens"] == max_tokens
    assert usage["total_tokens"] == len(prompt) + max_tokens


def test_a_repeated_prompt_reports_a_cache_hit_and_repeats_its_tokens(server: Server) -> None:
    prompt = list(range(100, 100 + 3 * CHUNK + 5))
    first, first_usage = _completion(server, prompt, 12)
    second, second_usage = _completion(server, prompt, 12)
    assert first_usage["prompt_tokens_details"]["cached_tokens"] == 0
    # Everything but the last prompt token is reused; that token yields the first logits.
    assert second_usage["prompt_tokens_details"]["cached_tokens"] == len(prompt) - 1
    assert _ids(second) == _ids(first)


@pytest.mark.parametrize("extra", [1, CHUNK - 1, CHUNK, 3 * CHUNK + 2])
def test_reusing_a_prefix_never_changes_the_output(
    server: Server, cold_server: Server, extra: int
) -> None:
    """A growing session prompt reuses the last chunk boundary and matches a cold prefill."""
    base = [7 + (i * 13) % 250 for i in range(2 * CHUNK + 3 + extra)]
    _completion(server, base, 2)
    grown = base + [9 + (i * 7) % 250 for i in range(extra + 4)]
    warm, warm_usage = _completion(server, grown, 16)
    cold, cold_usage = _completion(cold_server, grown, 16)
    cached = warm_usage["prompt_tokens_details"]["cached_tokens"]
    assert cached > 0
    assert cached % CHUNK == 0
    assert cold_usage["prompt_tokens_details"]["cached_tokens"] == 0
    assert _ids(warm) == _ids(cold)


def test_end_of_sequence_stops_unless_ignored(
    cold_server: Server, eos_token: tuple[list[int], int, int]
) -> None:
    ids, token, index = eos_token
    prompt = list(range(5, 40))
    stopped, usage = _completion(cold_server, prompt, 24, ignore_eos=False)
    assert _ids(stopped) == ids[:index]
    assert stopped[-1]["choices"][0]["finish_reason"] == "stop"
    assert usage["completion_tokens"] == index
    ignored, usage = _completion(cold_server, prompt, 24)
    assert _ids(ignored) == ids
    assert usage["completion_tokens"] == 24


def test_min_tokens_holds_back_end_of_sequence(
    cold_server: Server, eos_token: tuple[list[int], int, int]
) -> None:
    _, token, index = eos_token
    chunks, usage = _completion(
        cold_server, list(range(5, 40)), 24, ignore_eos=False, min_tokens=index + 1
    )
    assert token not in _ids(chunks)[: index + 1]
    assert usage["completion_tokens"] >= index + 1


def test_streaming_and_non_streaming_agree(server: Server) -> None:
    body = {
        "prompt": "def main():\n    return",
        "max_tokens": 10,
        "temperature": 0,
        "ignore_eos": True,
    }
    chunks = _stream(server, "/v1/completions", body)
    whole = httpx.post(f"{server.url}/v1/completions", json=body, timeout=_TIMEOUT).json()
    assert "".join(c["choices"][0]["text"] for c in chunks) == whole["choices"][0]["text"]
    assert whole["usage"]["completion_tokens"] == 10
    assert whole["choices"][0]["finish_reason"] == "length"


def test_a_stop_string_cuts_the_output_before_it(server: Server) -> None:
    body = {
        "prompt": "the capital of France is",
        "max_tokens": 24,
        "temperature": 0,
        "ignore_eos": True,
    }
    text = httpx.post(f"{server.url}/v1/completions", json=body, timeout=_TIMEOUT).json()[
        "choices"
    ][0]["text"]
    stop = text[5:7]
    assert stop
    cut = httpx.post(
        f"{server.url}/v1/completions", json={**body, "stop": [stop]}, timeout=_TIMEOUT
    ).json()
    assert cut["choices"][0]["text"] == text[: text.index(stop)]
    assert cut["choices"][0]["finish_reason"] == "stop"


def _chat(server: Server, **extra: Any) -> tuple[list[dict], dict]:
    body = {
        "model": server.model,
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        "max_completion_tokens": 6,
        "temperature": 0,
        "ignore_eos": True,
        "stream_options": {"include_usage": True},
        **extra,
    }
    *chunks, last = _stream(server, "/v1/chat/completions", body)
    return chunks, last["usage"]


def test_chat_defaults_to_non_thinking_and_honors_an_explicit_template_option(
    server: Server,
) -> None:
    default, default_usage = _chat(server)
    explicit, explicit_usage = _chat(server, chat_template_kwargs={"enable_thinking": False})
    thinking, thinking_usage = _chat(server, chat_template_kwargs={"enable_thinking": True})
    assert default_usage["prompt_tokens"] == explicit_usage["prompt_tokens"]
    assert thinking_usage["prompt_tokens"] < default_usage["prompt_tokens"]
    assert default[0]["choices"][0]["delta"]["role"] == "assistant"
    assert all("reasoning_content" not in c["choices"][0]["delta"] for c in default)
    assert any(c["choices"][0]["delta"].get("reasoning_content") for c in thinking)
    assert default_usage["completion_tokens"] == thinking_usage["completion_tokens"] == 6
    assert [c["choices"] for c in default] == [c["choices"] for c in explicit]


def test_chat_accepts_reasoning_and_tool_call_history(server: Server) -> None:
    messages = [
        {"role": "system", "content": "You are a coding agent."},
        {"role": "user", "content": [{"type": "text", "text": "List the files."}]},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "I should call the tool.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "ls", "arguments": '{"path": "."}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "main.py"},
    ]
    tools = [{"type": "function", "function": {"name": "ls", "parameters": {"type": "object"}}}]
    _, usage = _chat(server, messages=messages, tools=tools)
    assert usage["completion_tokens"] == 6
    _, short = _chat(server)
    assert usage["prompt_tokens"] > short["prompt_tokens"]


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"prompt": "a", "model": "another-model"}, 404),
        ({"prompt": "a", "max_tokens": 10**7}, 400),
        ({"prompt": [10**6]}, 400),
        ({"prompt": "a", "n": 2}, 400),
        ({"prompt": "a", "logprobs": 1}, 400),
        ({"prompt": {"text": "a"}}, 400),
    ],
    ids=["unknown-model", "too-long", "bad-token", "n", "logprobs", "bad-prompt"],
)
def test_requests_the_server_cannot_honor_are_refused(
    server: Server, body: dict, status: int
) -> None:
    response = httpx.post(f"{server.url}/v1/completions", json=body, timeout=_TIMEOUT)
    assert response.status_code == status
    assert response.json()["error"]["message"]


@pytest.mark.parametrize("hip", ["6.4.43484-123eb5128", "7.0.0"])
def test_rocm_builds_load_the_matmul_experts(hip: str) -> None:
    """torch 2.9.1+rocm6.4 raises on torch._grouped_mm, Transformers' default experts kernel."""
    assert experts_implementation(hip) == experts.NAME


def test_cuda_and_cpu_builds_keep_the_transformers_default() -> None:
    assert experts_implementation(None) is None


def test_the_rocm_experts_path_serves_exact_lengths(rocm_experts_server: Server) -> None:
    chunks, usage = _completion(rocm_experts_server, list(range(60, 90)), 9)
    assert len(_ids(chunks)) == usage["completion_tokens"] == 9


def _solo_and_together(
    server: Server, requests: list[tuple[list[int], int]]
) -> tuple[list, list, dict]:
    solo = [_ids(_completion(server, prompt, n)[0]) for prompt, n in requests]
    before = httpx.get(f"{server.url}/health").json()
    with ThreadPoolExecutor(len(requests)) as pool:
        futures = [pool.submit(_completion, server, prompt, n) for prompt, n in requests]
        together = [_ids(f.result(timeout=_TIMEOUT)[0]) for f in futures]
    after = httpx.get(f"{server.url}/health").json()
    return solo, together, {k: after[k] - before[k] for k in ("decode_steps", "decoded_tokens")}


@pytest.mark.parametrize("which", ["cold_server", "rocm_experts_server"])
def test_concurrent_requests_decode_together_and_match_their_solo_outputs(
    which: str, request: pytest.FixtureRequest
) -> None:
    """Different prompt lengths (padding) and output lengths (the batch shrinks as requests end).

    Also on the experts implementation ROCm uses, whose decode and prefill paths differ.
    """
    server = request.getfixturevalue(which)
    requests = [
        ([11 + (i * 17 + j * 5) % 250 for j in range(9 + 7 * i)], 6 + 5 * i) for i in range(6)
    ]
    solo, together, delta = _solo_and_together(server, requests)
    assert together == solo
    assert [len(ids) for ids in together] == [n for _, n in requests]
    # Fewer forward passes than tokens decoded: the requests shared decode steps.
    assert delta["decoded_tokens"] > delta["decode_steps"]


def test_batched_decode_keeps_prefix_cache_usage_accurate(server: Server) -> None:
    prompts = [[200 + (i * 31 + j) % 100 for j in range(3 * CHUNK + i)] for i in range(4)]
    for prompt in prompts:
        _completion(server, prompt, 2)
    with ThreadPoolExecutor(len(prompts)) as pool:
        futures = [pool.submit(_completion, server, prompt, 10) for prompt in prompts]
        results = [f.result(timeout=_TIMEOUT) for f in futures]
    for prompt, (chunks, usage) in zip(prompts, results, strict=True):
        assert usage["prompt_tokens_details"]["cached_tokens"] == len(prompt) - 1
        assert usage["completion_tokens"] == len(_ids(chunks)) == 10
