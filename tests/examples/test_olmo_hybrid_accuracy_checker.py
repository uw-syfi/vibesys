"""The olmo-hybrid-prefix-caching accuracy checker judges a running server, not a model.

Both tasks gate on one service-style checker (#4101): it sends the stored reference prompt
token IDs to ``/v1/completions`` and compares ``choices[0].text`` with the stored Hugging
Face reference, so it runs next to a loaded server on a 24 GB L4. The tests drive the
checker's public API against an in-memory completions server at its ``send`` seam, and
check that the stored reference is what ``regenerate_reference.py`` writes and what both
tasks run.
"""

from __future__ import annotations

import ast
import io
import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Protocol

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/olmo-hybrid-prefix-caching"
_SHARED = _PROJECT / ".vibesys/shared/accuracy_checker"
_CHECKER = SimpleNamespace(**runpy.run_path(str(_SHARED / "checker.py")))
_REGENERATE = SimpleNamespace(**runpy.run_path(str(_SHARED / "regenerate_reference.py")))
_DOCUMENT = json.loads((_SHARED / "reference_outputs.json").read_text())
_REFERENCE = _CHECKER.load_reference(_DOCUMENT)
_CASES = _REFERENCE.cases
_TASKS = ("from-scratch", "vllm")
_MODEL = "allenai/Olmo-Hybrid-7B"

#: What a completions call can go wrong with: the error ``send`` raises, or the body the
#: server answers with.
_FAULTS: dict[str, BaseException | object] = {
    "refused": ConnectionRefusedError(111, "Connection refused"),
    "timeout": TimeoutError("timed out"),
    "http-500": OSError("HTTP 500 from server"),
    "not-json": json.JSONDecodeError("Expecting value", "<html>", 0),
    "no-choices": {"choices": []},
    "two-choices": {"choices": [{"text": "a"}, {"text": "b"}]},
    "text-not-a-string": {"choices": [{"text": 1}]},
    "not-an-object": ["text"],
}


class _Request(Protocol):
    """The checker's ``CompletionRequest``."""

    url: str
    body: Mapping[str, object]


class _Case(Protocol):
    """The checker's ``Case``."""

    prompt_token_ids: tuple[int, ...]
    references: Mapping[str, str]


class _HttpStatusError(OSError):
    """What the checker's HTTP ``send`` raises for an error status."""

    def __init__(self, status: int, url: str) -> None:
        super().__init__(f"HTTP {status} from {url}")


class _CompletionsServer:
    """An in-memory completions endpoint that serves one model at ``<base>/v1/completions``.

    ``replies`` maps prompt token IDs to the completion text, or to a ``_FAULTS`` name.
    Like a real server, another URL fails with an HTTP error, and a request that would not
    run greedy, non-streaming, with EOS honored and under this model's name is refused.
    """

    def __init__(self, base_url: str, replies: Mapping[tuple[int, ...], str]) -> None:
        self.url = base_url.rstrip("/") + "/v1/completions"
        self.replies = replies
        self.requests: list[_Request] = []

    def send(self, request: _Request) -> object:
        self.requests.append(request)
        if request.url != self.url:
            raise _HttpStatusError(404, request.url)
        body = dict(request.body)
        prompt = body.get("prompt")
        greedy = (
            set(body) == {"model", "prompt", "max_tokens", "temperature", "stream"}
            and body["model"] == _MODEL
            and body["temperature"] == 0
            and body["stream"] is False
            and type(body["max_tokens"]) is int
            and isinstance(prompt, list)
            and all(type(token) is int for token in prompt)
        )
        if not greedy:
            raise _HttpStatusError(400, request.url)
        reply = self.replies[tuple(prompt)]
        fault = _FAULTS.get(reply)
        if isinstance(fault, BaseException):
            raise fault
        if fault is not None:
            return fault
        return {"choices": [{"index": 0, "text": reply, "finish_reason": "length"}]}


def _texts(case: _Case) -> list[str]:
    return list(case.references.values())


def _run(base_url: str, replies: Mapping[tuple[int, ...], str]) -> tuple[list[Any], int, str]:
    server = _CompletionsServer(base_url, replies)
    outcomes = _CHECKER.check(_CASES, base_url, _MODEL, server.send)
    out = io.StringIO()
    code = _CHECKER.report(_REFERENCE, base_url, outcomes, out)
    assert [request.url for request in server.requests] == [server.url] * len(_CASES)
    return outcomes, code, out.getvalue()


_BASE_URLS = st.builds(
    "".join,
    st.tuples(
        st.sampled_from(("http://localhost", "http://127.0.0.1", "https://server.test")),
        st.sampled_from(("", ":8000")),
        st.sampled_from(("", "/proxy")),
        st.sampled_from(("", "/")),
    ),
)
_PICKS = st.lists(st.integers(0, 1), min_size=len(_CASES), max_size=len(_CASES))


@pytest.mark.parametrize("task", _TASKS)
@pytest.mark.regression
def test_each_task_gates_on_a_checker_that_needs_only_the_standard_library(task: str) -> None:
    """A checker that imports torch, transformers or the candidate's ``main`` cannot run in
    the serving container next to the loaded server, which is where the Modal trusted
    evaluator runs it (#4101)."""
    [script] = [
        Path(part.replace("${PROJECT_ROOT}", str(_PROJECT)))
        for part in load_example_task(_PROJECT, task).accuracy_command
        if part.endswith(".py")
    ]
    imported = set()
    for node in ast.walk(ast.parse(script.read_text())):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add("." if node.level else (node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names), sorted(imported - set(sys.stdlib_module_names))


@given(base_url=_BASE_URLS, picks=_PICKS)
@example(base_url="http://localhost:8000", picks=[0] * len(_CASES))
@example(base_url="http://localhost:8000/", picks=[1] * len(_CASES))
def test_a_server_returning_a_reference_text_for_every_prompt_passes(
    base_url: str, picks: list[int]
) -> None:
    replies = {
        case.prompt_token_ids: _texts(case)[pick % len(_texts(case))]
        for case, pick in zip(_CASES, picks, strict=True)
    }
    server = _CompletionsServer(base_url, replies)

    outcomes = _CHECKER.check(_CASES, base_url, _MODEL, server.send)
    out = io.StringIO()

    assert _CHECKER.report(_REFERENCE, base_url, outcomes, out) == 0
    assert all(isinstance(outcome, _CHECKER.Passed) for outcome in outcomes)
    assert "ALL CHECKS PASSED" in out.getvalue()
    for request, case in zip(server.requests, _CASES, strict=True):
        assert request.url == base_url.rstrip("/") + "/v1/completions"
        assert request.body == {
            "model": _MODEL,
            "prompt": list(case.prompt_token_ids),
            "max_tokens": case.max_tokens,
            "temperature": 0.0,
            "stream": False,
        }


@given(
    base_url=_BASE_URLS,
    wrong=st.dictionaries(st.integers(0, len(_CASES) - 1), st.text(max_size=60), min_size=1),
)
def test_a_text_matching_no_reference_fails_exactly_that_prompt(
    base_url: str, wrong: dict[int, str]
) -> None:
    replies = {
        case.prompt_token_ids: wrong.get(index, _texts(case)[0])
        for index, case in enumerate(_CASES)
    }
    mismatched = {index for index, text in wrong.items() if text not in _texts(_CASES[index])}

    outcomes, code, output = _run(base_url, replies)

    assert {
        index for index, outcome in enumerate(outcomes) if isinstance(outcome, _CHECKER.Mismatched)
    } == mismatched
    assert all(
        isinstance(outcome, _CHECKER.Passed)
        for index, outcome in enumerate(outcomes)
        if index not in mismatched
    )
    assert code == (1 if mismatched else 0)
    assert ("ACCURACY CHECK FAILED" in output) == bool(mismatched)


@given(
    base_url=_BASE_URLS,
    faults=st.dictionaries(
        st.integers(0, len(_CASES) - 1), st.sampled_from(sorted(_FAULTS)), min_size=1
    ),
)
def test_a_failed_request_fails_that_prompt_and_the_others_still_run(
    base_url: str, faults: dict[int, str]
) -> None:
    replies = {
        case.prompt_token_ids: faults.get(index, _texts(case)[0])
        for index, case in enumerate(_CASES)
    }

    outcomes, code, output = _run(base_url, replies)

    for index, outcome in enumerate(outcomes):
        assert isinstance(outcome, _CHECKER.Failed if index in faults else _CHECKER.Passed)
    assert code == 1
    assert output.count("FAIL - request failed") == len(faults)


def test_a_server_at_another_path_fails_every_prompt() -> None:
    server = _CompletionsServer("http://localhost:8000", {})

    outcomes = _CHECKER.check(_CASES, "http://localhost:8000/v2", _MODEL, server.send)

    assert all(isinstance(outcome, _CHECKER.Failed) for outcome in outcomes)
    assert all("HTTP 404" in outcome.error for outcome in outcomes)
    assert _CHECKER.report(_REFERENCE, "http://localhost:8000/v2", outcomes, io.StringIO()) == 1


@pytest.mark.parametrize("timeout", ["0", "-1", "nan"])
def test_a_timeout_that_is_not_positive_is_refused(
    timeout: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        _CHECKER.main(["--timeout", timeout])
    assert "--timeout must be positive" in capsys.readouterr().err


# --- the stored reference -----------------------------------------------------------


def test_the_stored_reference_holds_the_prompts_regenerate_reference_generates() -> None:
    assert [(c.prompt, c.max_tokens, c.description) for c in _CASES] == list(_REGENERATE.PROMPTS)
    assert tuple(_REFERENCE.variants) == _REGENERATE.VARIANTS
    assert _DOCUMENT["generation"] == _REGENERATE.GENERATION


@pytest.mark.parametrize("task", _TASKS)
def test_each_task_serves_the_model_revision_the_reference_was_generated_from(task: str) -> None:
    meta = json.loads((task_root(_PROJECT, task) / "reference/meta.json").read_text())
    assert (meta["model_id"], meta["revision"]) == (_REFERENCE.model, _REFERENCE.revision)
    assert meta["model_id"] == _CHECKER.DEFAULT_MODEL


def _raw(variant: str, outputs: list[tuple[list[int], list[int], str]]) -> dict[str, object]:
    return {
        "variant": variant,
        "model": {"id": _MODEL, "revision": "0" * 40},
        "environment": {"gpu": "GPU", "flash_linear_attention": None},
        "cases": [
            {
                "description": description,
                "prompt": prompt,
                "max_tokens": max_tokens,
                "prompt_token_ids": prompt_ids,
                "output_token_ids": ids[:max_tokens],
                "output_text": text,
            }
            for (prompt, max_tokens, description), (prompt_ids, ids, text) in zip(
                _REGENERATE.PROMPTS, outputs, strict=True
            )
        ],
    }


_TOKENS = st.lists(st.integers(0, 100_351), min_size=1, max_size=8)
_OUTPUTS = st.lists(
    st.tuples(_TOKENS, st.lists(st.integers(0, 100_351), max_size=8), st.text(max_size=20)),
    min_size=len(_REGENERATE.PROMPTS),
    max_size=len(_REGENERATE.PROMPTS),
)


@given(
    plain=_OUTPUTS,
    texts=st.lists(
        st.text(max_size=20),
        min_size=len(_REGENERATE.PROMPTS),
        max_size=len(_REGENERATE.PROMPTS),
    ),
)
def test_merge_writes_a_reference_the_checker_loads(
    plain: list[tuple[list[int], list[int], str]], texts: list[str]
) -> None:
    fla = [(prompt_ids, ids, text) for (prompt_ids, ids, _), text in zip(plain, texts, strict=True)]

    reference = _CHECKER.load_reference(
        _REGENERATE.merge([_raw("flash_linear_attention", fla), _raw("torch", plain)])
    )

    assert tuple(reference.variants) == ("torch", "flash_linear_attention")
    for case, (prompt_ids, _, torch_text), fla_text in zip(
        reference.cases, plain, texts, strict=True
    ):
        assert case.prompt_token_ids == tuple(prompt_ids)
        assert dict(case.references) == {"torch": torch_text, "flash_linear_attention": fla_text}


def _outputs() -> list[tuple[list[int], list[int], str]]:
    return [([1, 2], [3], "x")] * len(_REGENERATE.PROMPTS)


def _other_revision() -> list[dict[str, object]]:
    other = _raw("flash_linear_attention", _outputs())
    other["model"] = {"id": _MODEL, "revision": "1" * 40}
    return [_raw("torch", _outputs()), other]


def _other_prompt_tokens() -> list[dict[str, object]]:
    return [_raw("torch", _outputs()), _raw("flash_linear_attention", [([9], [3], "x")] * 10)]


def _reordered() -> list[dict[str, object]]:
    raw = _raw("torch", _outputs())
    cases = raw["cases"]
    assert isinstance(cases, list)
    raw["cases"] = cases[::-1]
    return [raw]


@pytest.mark.parametrize(
    ("raws", "message"),
    [
        pytest.param(_other_revision, "used model", id="revision"),
        pytest.param(_other_prompt_tokens, "tokenized", id="prompt-tokens"),
        pytest.param(_reordered, "PROMPTS", id="order"),
        pytest.param(lambda: [_raw("torch", _outputs())] * 2, "two inputs", id="duplicate"),
        pytest.param(lambda: [_raw("cuda", _outputs())], "unknown variant", id="unknown"),
        pytest.param(list, "at least one", id="empty"),
    ],
)
def test_merge_refuses_outputs_that_disagree(
    raws: Callable[[], list[dict[str, object]]], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _REGENERATE.merge(raws())


def _with(path: tuple[object, ...], value: object) -> dict[str, Any]:
    document = json.loads(json.dumps(_DOCUMENT))
    target = document
    for key in path[:-1]:
        target = target[key]
    if value is _DELETE:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    return document


_DELETE = object()


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        pytest.param(("extra",), 1, r"reference: unknown keys \['extra'\]", id="top-key"),
        pytest.param(
            ("cases", 0, "outputs", "torch"), _DELETE, r"cases\[0\]\.outputs", id="variant"
        ),
        pytest.param(
            ("cases", 3, "prompt_token_ids"), ["1"], r"cases\[3\]\.prompt_token_ids", id="ids"
        ),
        pytest.param(("cases", 2, "max_tokens"), 0, r"cases\[2\]\.max_tokens", id="max-tokens"),
        pytest.param(
            ("cases", 1, "max_tokens"), 3, r"cases\[1\]\.outputs\.torch\.token_ids", id="long"
        ),
        pytest.param(
            ("cases", 0, "prompt"), _DELETE, r"cases\[0\]: .*missing keys \['prompt'\]", id="key"
        ),
        pytest.param(("model", "revision"), "", r"model\.id and model\.revision", id="revision"),
        pytest.param(("cases",), [], "cases must be a non-empty list", id="no-cases"),
    ],
)
def test_a_malformed_reference_is_refused_naming_the_key(
    path: tuple[object, ...], value: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        _CHECKER.load_reference(_with(path, value))
