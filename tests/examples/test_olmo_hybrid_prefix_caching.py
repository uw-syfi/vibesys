"""The olmo-hybrid-prefix-caching benchmark requests the endpoint it reports.

Each task's benchmark runs in process against an in-memory OpenAI-compatible server that,
like a real one, serves only its completions path and answers 404 everywhere else (#4100:
both benchmarks posted to the bare ``--url`` while reporting ``<url>/v1/completions``).
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.examples.example_task import task_root

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/olmo-hybrid-prefix-caching"
_TASKS = ("from-scratch", "vllm")
_BENCHMARKS = {
    task: SimpleNamespace(
        **runpy.run_path(str(task_root(_PROJECT, task) / "benchmark/benchmark.py"))
    )
    for task in _TASKS
}
_REQUESTS = 3
_WARMUP = 1
_MAX_TOKENS = 2


class _CompletionsServer:
    """A streaming completions server that answers only ``POST`` to one URL."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.requested: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requested.append(str(request.url))
        if request.method != "POST" or str(request.url) != self.url:
            return httpx.Response(404)
        body = json.loads(request.content)
        prompt = body.get("prompt")
        if body.get("stream") is not True or not isinstance(prompt, list) or not prompt:
            return httpx.Response(400)
        tokens = body["max_tokens"]
        usage = {"prompt_tokens": len(prompt), "completion_tokens": tokens}
        chunks: list[dict[str, object]] = [{"choices": [{"text": "t"}]} for _ in range(tokens)]
        chunks.append({"choices": [{"text": "", "finish_reason": "length"}], "usage": usage})
        stream = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"{stream}data: [DONE]\n\n".encode(),
        )


_BASE_URLS = st.builds(
    "".join,
    st.tuples(
        st.sampled_from(("http://localhost", "http://127.0.0.1", "https://server.test")),
        st.sampled_from(("", ":8000", ":18080")),
        st.sampled_from(("", "/proxy", "/a/b")),
        st.sampled_from(("", "/")),
    ),
)
_ENDPOINTS = st.one_of(
    st.none(), st.from_regex(r"/[a-z0-9]{1,8}(/[a-z0-9]{1,8}){0,2}", fullmatch=True)
)


@pytest.mark.regression
@pytest.mark.parametrize("task", _TASKS)
@given(base_url=_BASE_URLS, endpoint=_ENDPOINTS)
@example(base_url="http://localhost:8000", endpoint=None)
async def test_every_request_goes_to_the_endpoint_the_benchmark_records(
    task: str, base_url: str, endpoint: str | None
) -> None:
    benchmark = _BENCHMARKS[task]
    expected = base_url.rstrip("/") + ("/v1/completions" if endpoint is None else endpoint)
    server = _CompletionsServer(expected)
    argv = [
        "--url",
        base_url,
        *(() if endpoint is None else ("--endpoint", endpoint)),
        "--shared-len",
        "4",
        "--unique-len",
        "2",
        "--num-requests",
        str(_REQUESTS),
        "--warmup",
        str(_WARMUP),
        "--max-tokens",
        str(_MAX_TOKENS),
        "--seed",
        "7",
    ]

    result = await benchmark.run_benchmark(
        benchmark.parse_args(argv), transport=httpx.MockTransport(server.handle), vocab_size=1000
    )

    assert server.requested == [expected] * (_WARMUP + _REQUESTS)
    assert result["config"]["url"] == expected
    assert result["num_completed"] == _REQUESTS
    assert result["total_output_tokens"] == _REQUESTS * _MAX_TOKENS


@pytest.mark.parametrize("task", _TASKS)
@pytest.mark.parametrize("endpoint", ["v1/completions", "", "completions"])
def test_an_endpoint_without_a_leading_slash_is_refused(
    task: str, endpoint: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _BENCHMARKS[task].parse_args(["--endpoint", endpoint])
    assert "--endpoint must start with '/'" in capsys.readouterr().err
