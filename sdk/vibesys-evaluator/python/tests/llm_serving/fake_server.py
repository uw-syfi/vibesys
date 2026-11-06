"""An in-process OpenAI-compatible completions server that reproduces golden data, with faults.

``FakeServer`` answers the requests the golden gate sends from the golden data
itself: greedy continuations, echoed teacher-forced logprobs, streamed chunks
and cached-token usage. Its knobs inject the faults a real engine can have.
``FakeHandler`` serves it to ``urllib`` in this process (no socket), so the
HTTP client is exercised end to end.
"""

from __future__ import annotations

import email.message
import io
import json
import urllib.error
import urllib.request
import urllib.response
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping


def make_golden(*, cases: int = 3, prompt: int = 5, gen: int = 20, margin: float = 2.0) -> dict:
    """Synthetic golden data: deterministic tokens, logprobs and margins."""
    out = []
    for c in range(cases):
        prompt_ids = [100 + c * 10 + i for i in range(prompt)]
        greedy = [(7 * c + 3 * i) % 50 + 1 for i in range(gen)]
        out.append(
            {
                "name": f"case_{c}",
                "kind": "raw",
                "prompt_ids": prompt_ids,
                "greedy_ids": greedy,
                "tf_logprob": [-0.01 * (i + 1) for i in range(gen)],
                "tf_top1": list(greedy),
                "tf_margin": [margin] * gen,
            }
        )
    return {"meta": {"model": "org/model-1b", "source": "synthetic"}, "cases": out}


@dataclass
class FakeServer:
    """Serves ``golden``; each knob is a fault of a real engine."""

    golden: Mapping[str, Any]
    #: Added to every teacher-forced logprob (a numerics error).
    logprob_shift: float = 0.0
    #: Teacher-forced argmax replaced at these (case, position) pairs.
    flips: set[tuple[str, int]] = field(default_factory=set)
    #: Free-running greedy departs from golden at this position of each named case.
    diverge: dict[str, int] = field(default_factory=dict)
    #: Report this many cached prompt tokens (None: omit the field).
    cached_tokens: int | None = 0
    #: Leave out choices[0].token_ids (a server without return_token_ids).
    omit_token_ids: bool = False
    #: End the stream without data: [DONE].
    truncate_stream: bool = False
    requests: list[dict[str, Any]] = field(default_factory=list)

    def _case(self, ids: list[int]) -> tuple[dict, int]:
        """The case whose prompt starts ``ids``, and how many golden tokens follow the prompt."""
        for case in self.golden["cases"]:
            prompt = case["prompt_ids"]
            rest = ids[len(prompt) :]
            if ids[: len(prompt)] == prompt and rest == case["greedy_ids"][: len(rest)]:
                return case, len(rest)
        msg = f"fake server: no golden case for prompt {ids[:8]}"
        raise AssertionError(msg)

    def _greedy(self, ids: list[int], n: int) -> list[int]:
        case, start = self._case(ids)
        gold = list(case["greedy_ids"])
        at = self.diverge.get(case["name"])
        if at is not None and at < len(gold):
            gold[at] = gold[at] + 1000
        return gold[start : start + n]

    def complete(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(dict(body))
        ids = list(body["prompt"])
        if body.get("echo"):
            case, start = self._case(ids)
            lp: list[float | None] = [None] * len(case["prompt_ids"])
            top: list[dict[str, float] | None] = [None] * len(case["prompt_ids"])
            for i in range(start):
                lp.append(case["tf_logprob"][i] + self.logprob_shift)
                best = case["tf_top1"][i] + (5000 if (case["name"], i) in self.flips else 0)
                top.append({f"token_id:{best}": -0.001})
            choice = {"logprobs": {"token_logprobs": lp, "top_logprobs": top}}
            return {"choices": [choice]}
        tokens = self._greedy(ids, body["max_tokens"])
        choice: dict[str, Any] = {} if self.omit_token_ids else {"token_ids": tokens}
        usage: dict[str, Any] = {"prompt_tokens": len(ids), "completion_tokens": len(tokens)}
        if self.cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": min(self.cached_tokens, len(ids))}
        return {"choices": [choice], "usage": usage}

    def stream(self, body: Mapping[str, Any]) -> Iterator[str]:
        self.requests.append(dict(body))
        ids = list(body["prompt"])
        tokens = self._greedy(ids, body["max_tokens"])
        for token in tokens:
            yield "data: " + json.dumps({"choices": [{"token_ids": [token]}]})
            yield ""
        usage = {"prompt_tokens": len(ids), "completion_tokens": len(tokens)}
        yield "data: " + json.dumps({"choices": [], "usage": usage})
        if not self.truncate_stream:
            yield "data: [DONE]"


class _OkResponse(urllib.response.addinfourl):
    """A 200 response as http.client makes it, with the reason urllib's HTTPErrorProcessor reads."""

    msg = "OK"


class FakeHandler(urllib.request.BaseHandler):
    """Serves a ``FakeServer`` at ``http://fake-server`` to ``urllib`` without a socket."""

    def __init__(self, server: FakeServer, *, status: int = 200) -> None:
        self.server = server
        self.status = status

    def http_open(self, request: urllib.request.Request) -> urllib.response.addinfourl:
        assert request.full_url == "http://fake-server/v1/completions", request.full_url
        assert request.get_method() == "POST"
        headers = email.message.Message()
        if self.status != 200:
            raise urllib.error.HTTPError(
                request.full_url, self.status, "error", headers, io.BytesIO(b"model not loaded")
            )
        data = request.data
        assert isinstance(data, bytes)
        body = json.loads(data)
        if body.get("stream"):
            payload = "".join(line + "\n" for line in self.server.stream(body)).encode()
        else:
            payload = json.dumps(self.server.complete(body)).encode()
        return _OkResponse(io.BytesIO(payload), headers, request.full_url, 200)


def opener(server: FakeServer, *, status: int = 200) -> urllib.request.OpenerDirector:
    """An opener whose only HTTP handler is the fake server."""
    director = urllib.request.OpenerDirector()
    director.add_handler(FakeHandler(server, status=status))
    director.add_handler(urllib.request.HTTPErrorProcessor())
    return director
