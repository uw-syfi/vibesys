"""The golden-logprobs gate against an in-process server: correct numerics pass, faults fail.

The gate runs over HTTP (the package's urllib client) against ``FakeServer``
through an in-process handler, so the request bodies, the response parsing
and the verdict are all the real ones, with no socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

# test-isolation: logprobs() opens a real socket to the URL; the gate and its HTTP client are
# driven here through their private entry with an in-process urllib handler instead.
from vibesys_evaluator.llm_serving._golden import GoldenError, Target, failures, load, run_gate
from vibesys_evaluator.llm_serving._server import HttpCompletions, ServerError
from vibesys_evaluator.llm_serving.golden import Thresholds

from .fake_server import FakeServer, make_golden, opener

if TYPE_CHECKING:
    from collections.abc import Callable


def _gate(tmp_path: Path, server: FakeServer, *, status: int = 200) -> tuple[bool, dict]:
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(server.golden))
    golden = load(path)
    client = HttpCompletions("http://fake-server", opener=opener(server, status=status))
    lines: list[str] = []
    return run_gate(Target(client, golden.model), golden, Thresholds(), lines.append)


def test_a_server_that_reproduces_the_golden_data_passes(tmp_path: Path) -> None:
    server = FakeServer(make_golden())
    passed, summary = _gate(tmp_path, server)
    assert passed, failures(summary)
    assert failures(summary) == []
    assert summary["identical_cases"] == summary["cases"] == 3
    assert summary["stream_check"] == "ok"
    # Every request names the golden file's model, and the scoring request echoes the prompt.
    assert {r["model"] for r in server.requests} == {"org/model-1b"}
    assert any(r.get("echo") and r.get("logprobs") == 1 for r in server.requests)


@given(shift=st.floats(min_value=0.06, max_value=5.0))
def test_a_logprob_error_above_the_mean_bound_fails(
    tmp_path_factory: pytest.TempPathFactory, shift: float
) -> None:
    tmp_path = tmp_path_factory.mktemp("golden")
    passed, summary = _gate(tmp_path, FakeServer(make_golden(), logprob_shift=shift))
    assert not passed
    assert "mean |dlogprob| <= 0.05" in failures(summary)


@given(shift=st.floats(min_value=0.0, max_value=0.049))
def test_a_logprob_error_within_noise_passes(
    tmp_path_factory: pytest.TempPathFactory, shift: float
) -> None:
    tmp_path = tmp_path_factory.mktemp("golden")
    passed, summary = _gate(tmp_path, FakeServer(make_golden(), logprob_shift=shift))
    assert passed, failures(summary)


@given(case=st.integers(0, 2), position=st.integers(0, 11))
def test_one_decisive_argmax_flip_fails(
    tmp_path_factory: pytest.TempPathFactory, case: int, position: int
) -> None:
    tmp_path = tmp_path_factory.mktemp("golden")
    server = FakeServer(make_golden(), flips={(f"case_{case}", position)})
    passed, summary = _gate(tmp_path, server)
    assert not passed
    assert "decisive teacher-forced flips <= 0" in failures(summary)


@given(position=st.integers(0, 11))
def test_a_free_run_divergence_is_judged_by_the_golden_margin(
    tmp_path_factory: pytest.TempPathFactory, position: int
) -> None:
    tmp_path = tmp_path_factory.mktemp("golden")
    decisive = FakeServer(make_golden(margin=2.0), diverge={"case_1": position})
    passed, summary = _gate(tmp_path, decisive)
    assert not passed
    assert "free-run divergences only at near-ties" in failures(summary)
    assert "resume: chained-round divergences only at near-ties" in failures(summary)

    near_tie = FakeServer(make_golden(margin=0.1), diverge={"case_1": position})
    passed, summary = _gate(tmp_path_factory.mktemp("golden"), near_tie)
    assert passed, failures(summary)


def test_a_server_that_omits_cached_tokens_fails_the_resume_usage_check(tmp_path: Path) -> None:
    passed, summary = _gate(tmp_path, FakeServer(make_golden(), cached_tokens=None))
    assert not passed
    assert failures(summary) == ["resume: cached_tokens reported, 0 <= cached <= prompt tokens"]


def test_a_stream_without_done_fails(tmp_path: Path) -> None:
    passed, summary = _gate(tmp_path, FakeServer(make_golden(), truncate_stream=True))
    assert not passed
    assert failures(summary) == ["stream: stream did not end with data: [DONE]"]


def test_a_server_without_token_ids_is_named_as_such(tmp_path: Path) -> None:
    with pytest.raises(ServerError, match="return_token_ids"):
        _gate(tmp_path, FakeServer(make_golden(), omit_token_ids=True))


def test_an_error_status_carries_the_servers_body(tmp_path: Path) -> None:
    with pytest.raises(ServerError, match="503 from the server: model not loaded"):
        _gate(tmp_path, FakeServer(make_golden()), status=503)


@pytest.mark.parametrize(
    ("damage", "named"),
    [
        pytest.param(lambda g: g.pop("meta"), "meta.model", id="no-meta"),
        pytest.param(lambda g: g.update(cases=[]), "non-empty", id="no-cases"),
        pytest.param(lambda g: g["cases"][0].pop("tf_margin"), "lacks tf_margin", id="no-margin"),
        pytest.param(lambda g: g["cases"][1]["tf_logprob"].pop(), "tf_logprob", id="short"),
        pytest.param(lambda g: g["cases"][0].update(prompt_ids=[1, -2]), "token ids", id="neg"),
        pytest.param(lambda g: g["cases"][2].update(name="case_0"), "unique", id="dup-name"),
    ],
)
def test_a_damaged_golden_file_is_refused_naming_what_is_wrong(
    tmp_path: Path, damage: Callable[[dict], object], named: str
) -> None:
    golden = make_golden()
    damage(golden)
    path = tmp_path / "golden.json"
    path.write_text(json.dumps(golden))
    with pytest.raises(GoldenError, match=named):
        load(path)


def test_the_tasks_golden_file_loads() -> None:
    repo = Path(__file__).resolve().parents[5]
    task = repo / "examples/model-serving/qwen3.5-9b-mi210/.vibesys/tasks/agentx/golden.json"
    golden = load(task)
    assert golden.model == "Qwen/Qwen3.5-9B"
    assert len(golden.cases) == 12
