from __future__ import annotations

import csv
import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from vibesys.evaluators.input_manifest import load_input_bundle

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE = _REPO_ROOT / "examples" / "model-serving" / "qwen3.5-9b-mi210"
_ASSETS_ENV = "QWEN35_BENCH_ASSETS"


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the bundle's stdlib-only benchmark/run.py as a module."""
    name = "qwen35_mi210_run"
    spec = importlib.util.spec_from_file_location(name, _BUNDLE / "benchmark" / "run.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_manifest_runs_quick_mode_through_the_adapter() -> None:
    bundle = load_input_bundle(_BUNDLE)
    manifest = bundle.manifest

    assert manifest.evaluator is not None
    assert manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert manifest.benchmark.entrypoint == "request-factory-adapter"
    assert manifest.benchmark.args == ("benchmark/run.py", "--mode", "quick")
    assert manifest.benchmark.result is not None
    assert manifest.benchmark.result.json_argument == "--output-json"
    assert manifest.benchmark.result.metric == "output_tokens_per_s"
    assert manifest.accuracy.command == ("uv", "run", "python", "accuracy_checker/checker.py")
    assert (_BUNDLE / "accuracy_checker" / "golden.json").is_file()


def _sessions(trace: Path) -> list[str]:
    rows = list(csv.DictReader(trace.open(newline="")))
    assert rows[0]["arrival_time_ms"] == "0.000000"
    return list(dict.fromkeys(row["session_id"] for row in rows))


def test_checked_in_slices_cover_every_mode_and_the_held_out_range(run: ModuleType) -> None:
    traces = _BUNDLE / "benchmark" / "traces"

    assert _sessions(run.DEFAULT_TRACE) == [f"synthetic_{i:06d}" for i in range(260)]
    assert max(run.MODE_SESSIONS.values()) <= 260
    assert run.WARMUP_SESSIONS <= 260
    held_out = _sessions(traces / "coding_session_3000-3299.csv")
    assert held_out == [f"synthetic_{i:06d}" for i in range(3000, 3300)]
    assert traces / "coding_session_3000-3299.csv" == run.HOLDOUT_TRACE
    # holdout measures only the first MODE_SESSIONS["holdout"] sessions of that
    # file (3000-3259); the remaining 3260-3299 are unused headroom.
    assert run.MODE_SESSIONS["holdout"] == run.MODE_SESSIONS["full"]
    assert held_out[: run.MODE_SESSIONS["holdout"]] == [
        f"synthetic_{i:06d}" for i in range(3000, 3260)
    ]


def test_warmup_pool_is_disjoint_from_every_mode_s_measured_sessions(run: ModuleType) -> None:
    # Regression coverage: before WARMUP_TRACE existed, quick/full warmed up on
    # sessions 0-11 of their own measured trace, so those 12 sessions started
    # the measured sub-run pre-cached (see README.md "Warmup"). Every mode's
    # warmup and measured session ids must now be disjoint.
    warmup_ids = set(_sessions(run.WARMUP_TRACE))
    assert len(warmup_ids) == run.WARMUP_SESSIONS

    measured_by_mode = {
        "quick": set(_sessions(run.DEFAULT_TRACE)[: run.MODE_SESSIONS["quick"]]),
        "full": set(_sessions(run.DEFAULT_TRACE)[: run.MODE_SESSIONS["full"]]),
        "holdout": set(_sessions(run.HOLDOUT_TRACE)[: run.MODE_SESSIONS["holdout"]]),
    }
    for mode, measured_ids in measured_by_mode.items():
        assert warmup_ids.isdisjoint(measured_ids), mode

    # And the two measured ranges quick/full and holdout draw from must
    # themselves be disjoint (the whole point of a held-out set).
    assert measured_by_mode["full"].isdisjoint(measured_by_mode["holdout"])


def test_default_inputs_are_the_verified_slice_and_fetched_corpus(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)
    built = tmp_path / "corpus.txt"
    monkeypatch.setattr(run.fetch_corpus, "build", lambda _path: built)
    args = run.parse_args(["--mode", "quick", "--request-factory-engine", "rf"])

    assert run.resolve_trace(args, "quick") == run.DEFAULT_TRACE
    assert run.resolve_corpus(args) == built


def test_holdout_mode_defaults_to_the_checked_in_holdout_trace(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)
    args = run.parse_args(["--mode", "holdout", "--request-factory-engine", "rf"])

    assert run.resolve_trace(args, "holdout") == run.HOLDOUT_TRACE


def test_resolve_paths_attaches_the_checked_in_warmup_trace(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)
    built = tmp_path / "corpus.txt"
    monkeypatch.setattr(run.fetch_corpus, "build", lambda _path: built)
    args = run.parse_args(
        [
            "--mode",
            "holdout",
            "--request-factory-engine",
            "rf",
            "--tokenizer",
            str(tmp_path),
        ]
    )

    paths = run.resolve_paths(args, "holdout")

    assert paths.trace == run.HOLDOUT_TRACE
    assert paths.warmup_trace == run.WARMUP_TRACE


def test_a_modified_default_trace_is_rejected(run: ModuleType, tmp_path: Path) -> None:
    tampered = tmp_path / "trace.csv"
    tampered.write_bytes(run.DEFAULT_TRACE.read_bytes().replace(b",306,", b",307,", 1))

    with pytest.raises(run.HarnessError, match=r"slice_trace\.py"):
        run.verify_default_trace(tampered)


def test_configured_inputs_must_exist(
    run: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_ASSETS_ENV, str(tmp_path))
    engine = tmp_path / "session_runner"

    assert run.main(["--mode", "smoke", "--request-factory-engine", str(engine)]) == 1
    missing = tmp_path / "coding_session_synthetic.csv"
    assert f"--trace does not exist: {missing}" in capsys.readouterr().err


def test_unreachable_corpus_download_names_the_fallbacks(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)

    failure = run.fetch_corpus.CorpusError("GET failed")

    def offline(_path: Path) -> Path:
        raise failure

    monkeypatch.setattr(run.fetch_corpus, "build", offline)
    args = run.parse_args(["--mode", "quick", "--request-factory-engine", "rf"])

    with pytest.raises(run.HarnessError, match=r"fetch_corpus\.py") as error:
        run.resolve_corpus(args)
    assert _ASSETS_ENV in str(error.value)


def test_corpus_order_uses_only_pinned_ebooks(run: ModuleType) -> None:
    pinned = dict(run.fetch_corpus.EBOOKS)

    assert set(run.fetch_corpus.ORDER) == set(pinned)
    assert run.fetch_corpus.ORDER.count(1342) == 2


def test_measured_metrics_read_the_session_runner_summary(run: ModuleType) -> None:
    summary = {
        "replay": {
            "common": {
                "run_duration_ms": 2000.0,
                "actual_output_tokens": 400,
                "output_token_throughput_per_s": 200.0,
                "request_throughput_per_s": 3.0,
                "ttft_ms_p50": 10.0,
                "attempted_steps": 6,
                "success_steps": 6,
                "failed_steps": 0,
            },
            "prefix_cache": {
                "measured_server_prompt_tokens": 1600,
                "server_prefix_hit_rate": 0.5,
                "planned_prefix_hit_rate": 0.78,
            },
        }
    }

    metrics = run.measured_metrics(summary)

    assert metrics["output_tokens_per_s"] == 200.0
    assert metrics["total_tokens_per_s"] == 1000.0
    assert metrics["ttft_ms"]["p50"] == 10.0
    assert metrics["prefix_cache_hit_rate_server"] == 0.5
    assert metrics["failed_steps"] == 0


# ----------------------------------------------------------------------------- accuracy gate


@pytest.fixture
def checker(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """Import this bundle's accuracy_checker.checker (other bundles ship one too)."""

    def ours() -> list[str]:
        return [m for m in sys.modules if m.split(".")[0] == "accuracy_checker"]

    for name in ours():
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.syspath_prepend(str(_BUNDLE))
    yield importlib.import_module("accuracy_checker.checker")
    for name in ours():
        del sys.modules[name]


def _golden(near_tie_at: int | None = None) -> dict[str, Any]:
    """Two synthetic cases of 8 golden tokens (4 rounds of 2); decisive unless near_tie_at."""

    def case(name: str, prompt: list[int], gold: list[int]) -> dict[str, Any]:
        return {
            "name": name,
            "prompt_ids": prompt,
            "greedy_ids": gold,
            "tf_logprob": [-0.1] * len(gold),
            "tf_top1": gold,
            "tf_margin": [2.0] * len(gold),
        }

    golden = {
        "cases": [
            case("short", [1, 2, 3], list(range(10, 18))),
            case("long", [4, 5, 6, 7, 8, 9], list(range(20, 28))),
        ]
    }
    if near_tie_at is not None:
        golden["cases"][0]["tf_margin"][near_tie_at] = 0.1
    return golden


@dataclass(frozen=True)
class Behavior:
    prefix_cache: bool = True
    report_cached: bool = True
    # Resume bug: on a cache hit, generation starts one position late.
    off_by_one_on_hit: bool = False
    # Continuation offsets (in the "short" case) where the server emits another token.
    flip_at: frozenset[int] = frozenset()


def _common_prefix(a: list[int], b: list[int]) -> int:
    return next(
        (i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), min(len(a), len(b))
    )


class StubServer:
    """In-memory OpenAI-compatible completions server that decodes the golden sequence."""

    def __init__(self, golden: dict[str, Any], behavior: Behavior) -> None:
        self.cases = golden["cases"]
        self.behavior = behavior
        self.computed: list[list[int]] = []  # sequences whose state the server holds

    def _case(self, tokens: list[int]) -> dict[str, Any]:
        for case in self.cases:
            full = case["prompt_ids"] + case["greedy_ids"]
            if len(tokens) >= len(case["prompt_ids"]) and full[: len(tokens)] == tokens:
                return case
        pytest.fail(f"unknown prompt {tokens}")

    def _cached(self, prompt: list[int]) -> int:
        if not self.behavior.prefix_cache:
            return 0
        hits = [min(_common_prefix(seq, prompt), len(prompt) - 1) for seq in self.computed]
        return max([0, *hits])

    def _generate(self, prompt: list[int], n: int) -> tuple[list[int], int]:
        case = self._case(prompt)
        cached = self._cached(prompt)
        start = len(prompt) - len(case["prompt_ids"])
        if self.behavior.off_by_one_on_hit and cached:
            start += 1
        gold = case["greedy_ids"] + [0] * (n + 1)
        flips = self.behavior.flip_at if case["name"] == "short" else frozenset()
        out = [gold[i] + 1000 if i in flips else gold[i] for i in range(start, start + n)]
        self.computed.append(prompt + out[:-1])
        return out, cached

    def _usage(self, prompt: list[int], n: int, cached: int) -> dict[str, Any]:
        usage: dict[str, Any] = {"prompt_tokens": len(prompt), "completion_tokens": n}
        if self.behavior.report_cached:
            usage["prompt_tokens_details"] = {"cached_tokens": cached}
        return usage

    def _echo(self, tokens: list[int]) -> dict[str, Any]:
        case = self._case(tokens)
        prompt_len = len(case["prompt_ids"])
        given: list[float | None] = [None] + [0.0] * (prompt_len - 1)
        top: list[dict[str, float] | None] = [None] + [{"token_id:0": 0.0}] * (prompt_len - 1)
        for lp, top1 in zip(case["tf_logprob"], case["tf_top1"], strict=True):
            given.append(lp)
            top.append({f"token_id:{top1}": lp})
        return {"choices": [{"logprobs": {"token_logprobs": given, "top_logprobs": top}}]}

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompt, n = body["prompt"], body["max_tokens"]
        if body.get("echo"):
            return httpx.Response(200, json=self._echo(prompt))
        out, cached = self._generate(prompt, n)
        usage = self._usage(prompt, n, cached)
        if not body.get("stream"):
            return httpx.Response(200, json={"choices": [{"token_ids": out}], "usage": usage})
        chunks: list[dict[str, Any]] = [{"choices": [{"token_ids": [t]}]} for t in out]
        chunks.append({"choices": [], "usage": usage})
        sse = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
        return httpx.Response(200, text=sse)


def _run_gate(
    checker: ModuleType, behavior: Behavior, golden: dict[str, Any] | None = None
) -> tuple[bool, dict[str, Any]]:
    golden = golden or _golden()
    target = checker.HttpTarget("http://stub", "Qwen/Qwen3.5-9B")
    server = StubServer(golden, behavior)
    target.client = httpx.Client(transport=httpx.MockTransport(server.handle))
    return checker.run_gate(target, golden, checker.Thresholds(), log=lambda _line: None)


@pytest.mark.usefixtures("checker")
def test_resume_rounds_split_the_golden_continuation() -> None:
    resume = importlib.import_module("accuracy_checker.resume")

    assert resume.plan_rounds(64, 4) == [(0, 16), (16, 16), (32, 16), (48, 16)]
    assert resume.plan_rounds(10, 4) == [(0, 3), (3, 3), (6, 3), (9, 1)]


def test_gate_passes_a_correct_server_that_resumes_from_its_cache(checker: ModuleType) -> None:
    passed, summary = _run_gate(checker, Behavior())

    resume = summary["resume_check"]
    assert passed
    # Rounds 2-4 of both cases resume from the previous round's computed context.
    assert resume["cache_hit_rounds"] == 6
    assert resume["cached_tokens"] == resume["resumable_tokens"] > 0


def test_gate_passes_a_correct_server_without_prefix_caching(checker: ModuleType) -> None:
    passed, summary = _run_gate(checker, Behavior(prefix_cache=False))

    assert passed
    assert summary["resume_check"]["cache_hit_rounds"] == 0


def test_gate_fails_a_server_whose_cache_hits_resume_at_the_wrong_position(
    checker: ModuleType,
) -> None:
    passed, summary = _run_gate(checker, Behavior(off_by_one_on_hit=True))

    resume = summary["resume_check"]
    assert not passed
    assert not resume["checks"]["resume: chained-round divergences only at near-ties"]
    assert "short#2@2 (cached 4)" in resume["cache_hit_non_tie_divergences"]


def test_gate_fails_a_server_that_omits_cached_tokens(checker: ModuleType) -> None:
    passed, summary = _run_gate(checker, Behavior(report_cached=False))

    checks = summary["resume_check"]["checks"]
    assert not passed
    assert not checks["resume: cached_tokens reported, 0 <= cached <= prompt tokens"]


def test_gate_tolerates_a_chained_divergence_only_at_a_near_tie(checker: ModuleType) -> None:
    flip = Behavior(flip_at=frozenset({5}))

    decisive_passed, decisive = _run_gate(checker, flip)
    tie_passed, tie = _run_gate(checker, flip, _golden(near_tie_at=5))

    assert not decisive_passed
    assert decisive["resume_check"]["non_tie_divergences"] == ["short#3@5 (cached 6)"]
    assert tie_passed
    assert tie["resume_check"]["non_tie_divergences"] == []
