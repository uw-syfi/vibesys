"""AgentX replay: the recipe, the harness pin, and reading AIPerf's export failing closed.

The harness is a Fake runner that writes an export where the command line
says; exports are the constructed fixture and every invalid shape of it.
"""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

# test-isolation: replay() runs the real harness; its recipe, pin check and export reader are
# driven through the private module with a Fake runner and an injected installed version.
from vibesys_evaluator._private.harness import HarnessError
from vibesys_evaluator._private.records import RecordStream
from vibesys_evaluator.llm_serving import agentx
from vibesys_evaluator.llm_serving._agentx import (
    HF_DATASET,
    AgentxError,
    Replay,
    command,
    datasets_cache,
    harness_environment,
    run,
    summary_metrics,
)
from vs_evaluator_protocol.api import parse_records, read_measurement

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

FIXTURE = Path(__file__).with_name("fixtures") / "profile_export_aiperf.json"
EXPECTED = {
    "output_tokens_per_s": 412.5,
    "p90_latency_ms": 18250.0,
    "ttft_p50_ms": 310.25,
    "ttft_p90_ms": 1840.0,
    "tpot_p50_ms": 21.5,
    "tpot_p90_ms": 34.75,
}
REVISION = "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
#: Where each metric is read: (export key, statistic, unit).
SOURCES = {
    "output_tokens_per_s": ("output_token_throughput", "avg", "tokens/sec"),
    "p90_latency_ms": ("request_latency", "p90", "ms"),
    "ttft_p50_ms": ("time_to_first_token", "p50", "ms"),
    "ttft_p90_ms": ("time_to_first_token", "p90", "ms"),
    "tpot_p50_ms": ("inter_token_latency", "p50", "ms"),
    "tpot_p90_ms": ("inter_token_latency", "p90", "ms"),
}


def _export() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text())


def _replay(mode: str = "full", **changes: object) -> Replay:
    fields: dict[str, Any] = {
        "model": "Qwen/Qwen3.5-9B",
        "tokenizer": "Qwen/Qwen3.5-9B",
        "tokenizer_revision": REVISION,
        "url": "http://127.0.0.1:41234",
        "concurrency": 4,
        "max_context_length": 262144,
        "mode": mode,
        "timeout_secs": 3300.0,
        **changes,
    }
    return Replay(**fields)


class FakeHarness:
    """Writes ``exports`` copies of ``summary`` under the directory the command names."""

    def __init__(
        self, summary: object, *, exports: int = 1, error: Exception | None = None
    ) -> None:
        self.summary = summary
        self.exports = exports
        self.error = error
        self.calls: list[tuple[list[str], float, Path]] = []

    def __call__(self, argv: Sequence[str], *, timeout_secs: float, cwd: Path) -> None:
        self.calls.append((list(argv), timeout_secs, cwd))
        if self.error is not None:
            raise self.error
        destination = Path(argv[argv.index("--output-artifact-dir") + 1])
        for index in range(self.exports):
            folder = destination / f"run-{index}"
            folder.mkdir(parents=True)
            text = self.summary if isinstance(self.summary, str) else json.dumps(self.summary)
            (folder / "profile_export_aiperf.json").write_text(text)


def test_the_fixture_export_reads_as_the_six_metrics(tmp_path: Path) -> None:
    harness = FakeHarness(_export())
    values = run(_replay(), tmp_path / "agentx", runner=harness, installed="1.0.7")
    assert values == EXPECTED
    assert set(values) == set(agentx.METRICS)
    argv, timeout, cwd = harness.calls[0]
    assert argv[1:5] == ["-I", "-m", "aiperf", "profile"]
    assert timeout == 3300.0
    assert cwd == tmp_path / "agentx"


def test_the_public_constants_are_the_pins() -> None:
    assert agentx.HARNESS_VERSION == "1.0.7"
    assert {name: m.direction for name, m in agentx.METRICS.items()} == {
        "output_tokens_per_s": "max",
        "p90_latency_ms": "min",
        "ttft_p50_ms": "min",
        "ttft_p90_ms": "min",
        "tpot_p50_ms": "min",
        "tpot_p90_ms": "min",
    }


@pytest.mark.parametrize(
    ("installed", "named"),
    [
        pytest.param(None, "agentx-harness is not installed", id="absent"),
        pytest.param("1.0.6", "agentx-harness 1.0.6 is installed", id="older"),
        pytest.param("1.0.8", "agentx-harness 1.0.8 is installed", id="newer"),
        pytest.param("1.0.7.post1", "1.0.7.post1 is installed", id="post"),
    ],
)
def test_any_harness_but_the_pinned_one_is_refused_before_it_runs(
    tmp_path: Path, installed: str | None, named: str
) -> None:
    harness = FakeHarness(_export())
    with pytest.raises(AgentxError, match=named):
        run(_replay(), tmp_path / "agentx", runner=harness, installed=installed)
    assert harness.calls == []


def _set(path: str, value: object = None) -> Callable[[dict[str, Any]], None]:
    def change(export: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node = export
        for key in parents:
            node = node[key]
        node[leaf] = value

    return change


def _drop(path: str) -> Callable[[dict[str, Any]], None]:
    def change(export: dict[str, Any]) -> None:
        *parents, leaf = path.split(".")
        node = export
        for key in parents:
            node = node[key]
        del node[leaf]

    return change


INVALID = [
    pytest.param(_set("metadata.scenario", "chat"), "scenario must be 'agentx'", id="scenario"),
    pytest.param(_drop("metadata"), "scenario must be 'agentx'", id="no-metadata"),
    pytest.param(
        _set("metadata.submission_valid", value=False), "submission invalid", id="invalid"
    ),
    pytest.param(_drop("metadata.submission_valid"), "submission invalid", id="no-valid"),
    pytest.param(_set("was_cancelled", value=True), "cancelled", id="cancelled"),
    pytest.param(_drop("error_summary"), "explicit empty list", id="no-error-summary"),
    pytest.param(_set("error_summary", None), "explicit empty list", id="null-errors"),
    pytest.param(_set("error_summary", [{"code": 500}]), "explicit empty list", id="errors-listed"),
    pytest.param(_set("error_request_count.avg", 2), "requests failed", id="error-count"),
    pytest.param(_set("request_count.avg", 0), "no measured requests", id="no-requests"),
    pytest.param(_set("output_token_throughput.avg", 0), "must be positive", id="zero-tput"),
    pytest.param(_set("output_token_throughput.unit", "tokens/s"), "unit must be", id="unit"),
    pytest.param(_set("time_to_first_token.unit", "s"), "unit must be 'ms'", id="ttft-unit"),
    pytest.param(_set("inter_token_latency.p90", float("nan")), "finite", id="nan"),
    pytest.param(_set("request_latency.p90", float("inf")), "finite", id="inf"),
    pytest.param(_set("time_to_first_token.p50", -1.0), "non-negative", id="negative"),
    pytest.param(_set("inter_token_latency.p50", value=True), "must be a number", id="bool"),
    pytest.param(_set("request_latency.p90", "12"), "must be a number", id="string"),
    pytest.param(_drop("inter_token_latency"), "inter_token_latency.unit", id="missing"),
]


@pytest.mark.parametrize(("damage", "named"), INVALID)
def test_every_invalid_export_shape_fails_closed_naming_it(
    tmp_path: Path, damage: Callable[[dict[str, Any]], None], named: str
) -> None:
    export = _export()
    damage(export)
    # json.dumps writes NaN and Infinity, which AIPerf's export can carry.
    with pytest.raises(AgentxError, match=named):
        run(_replay(), tmp_path / "agentx", runner=FakeHarness(export), installed="1.0.7")


def test_an_omitted_error_counter_is_zero_only_beside_an_explicit_empty_summary() -> None:
    export = _export()
    del export["error_request_count"]
    assert summary_metrics(export, smoke=False) == EXPECTED
    del export["error_summary"]
    with pytest.raises(AgentxError, match="explicit empty list"):
        summary_metrics(export, smoke=False)


def test_smoke_is_not_held_to_submission_validity() -> None:
    export = _export()
    export["metadata"]["submission_valid"] = False
    assert summary_metrics(export, smoke=True) == EXPECTED
    with pytest.raises(AgentxError, match="submission invalid"):
        summary_metrics(export, smoke=False)


@pytest.mark.parametrize(
    ("exports", "text", "named"),
    [
        pytest.param(0, None, "found 0", id="none"),
        pytest.param(2, None, "found 2", id="two"),
        pytest.param(1, "{not json", "not readable JSON", id="garbage"),
        pytest.param(1, "[1, 2]", "must be a JSON object", id="array"),
    ],
)
def test_the_export_must_be_one_json_object(
    tmp_path: Path, exports: int, text: str | None, named: str
) -> None:
    harness = FakeHarness(text if text is not None else _export(), exports=exports)
    with pytest.raises(AgentxError, match=named):
        run(_replay(), tmp_path / "agentx", runner=harness, installed="1.0.7")


def test_a_used_artifact_directory_is_refused(tmp_path: Path) -> None:
    artifacts = tmp_path / "agentx"
    artifacts.mkdir()
    (artifacts / "stale.json").write_text("{}")
    harness = FakeHarness(_export())
    with pytest.raises(AgentxError, match="must be empty"):
        run(_replay(), artifacts, runner=harness, installed="1.0.7")
    assert harness.calls == []


def test_a_harness_failure_ends_the_replay(tmp_path: Path) -> None:
    harness = FakeHarness(_export(), error=HarnessError("python -I -m aiperf ... exited 3"))
    with pytest.raises(HarnessError, match="exited 3"):
        run(_replay(), tmp_path / "agentx", runner=harness, installed="1.0.7")


def test_full_and_smoke_recipes() -> None:
    full = command(_replay("full"), Path("/a"), python="py")
    smoke = command(_replay("smoke"), Path("/a"), python="py")
    assert full[full.index("--benchmark-duration") + 1] == "900"
    assert full[full.index("--warmup-requests-per-lane") + 1] == "10"
    assert "--unsafe-override" not in full
    assert smoke[smoke.index("--benchmark-duration") + 1] == "60"
    assert smoke[-3:] == ["--num-dataset-entries", "2", "--unsafe-override"]
    for argv in (full, smoke):
        assert argv[argv.index("--public-dataset") + 1] == "semianalysis_cc_traces_weka_062126"
        assert argv[argv.index("--random-seed") + 1] == "42"
        assert argv[argv.index("--url") + 1] == "http://127.0.0.1:41234"
        assert argv[argv.index("--output-artifact-dir") + 1] == "/a"


@given(
    concurrency=st.integers(1, 512),
    context=st.integers(1, 1 << 20),
    model=st.text(min_size=1, max_size=40).filter(str.strip),
)
def test_the_recipe_carries_the_tasks_policy_verbatim(
    concurrency: int, context: int, model: str
) -> None:
    replay = _replay(model=model, concurrency=concurrency, max_context_length=context)
    argv = command(replay, Path("/out"), python="py")
    assert argv[argv.index("--model") + 1] == model
    assert argv[argv.index("--concurrency") + 1] == str(concurrency)
    assert argv[argv.index("--max-context-length") + 1] == str(context)


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        pytest.param({"tokenizer_revision": "main"}, "40-hex-digit", id="revision"),
        pytest.param({"url": "127.0.0.1:8000"}, "http", id="url"),
        pytest.param({"concurrency": 0}, "concurrency", id="concurrency"),
        pytest.param({"max_context_length": True}, "max_context_length", id="bool"),
        pytest.param({"mode": "fast"}, "'full' or 'smoke'", id="mode"),
        pytest.param({"model": " "}, "model", id="model"),
        pytest.param({"timeout_secs": math.inf}, "timeout_secs", id="timeout"),
    ],
)
def test_a_replay_the_task_did_not_mean_is_refused(changes: dict[str, Any], named: str) -> None:
    with pytest.raises(ValueError, match=named):
        _replay(**changes)


finite = st.floats(min_value=0.0, max_value=1e9, allow_nan=False, allow_infinity=False)


@given(
    values=st.fixed_dictionaries(
        dict.fromkeys(SOURCES, finite),
    ),
    requests=st.integers(1, 10**6),
)
def test_any_valid_export_translates_to_a_protocol_2_result(
    tmp_path_factory: pytest.TempPathFactory, values: dict[str, float], requests: int
) -> None:
    """Translation is exact, and the result it makes is one the protocol's reader accepts."""
    values = {**values, "output_tokens_per_s": values["output_tokens_per_s"] + 1.0}
    export = copy.deepcopy(_export())
    export["request_count"]["avg"] = requests
    for name, (key, statistic, unit) in SOURCES.items():
        export[key]["unit"] = unit
        export[key][statistic] = values[name]
    assert summary_metrics(export, smoke=False) == values

    output = tmp_path_factory.mktemp("records") / "records.jsonl"
    stream = RecordStream(output)
    stream.hello(agentx.METRICS)
    stream.result(summary_metrics(export, smoke=False))
    stream.close()
    measurement = read_measurement(parse_records(output.read_text()))
    assert measurement.values == values


# The harness's datasets cache. Regression: vibesys-cli mounts the operator's Hugging Face cache
# read-only, and `datasets` takes a lock file in its cache even to read a prepared dataset, so the
# replay failed with "Read-only file system" before it sent a request.

CACHED = "semianalysisai___cc-traces-weka-062126"


def _read_only(_: Path) -> bool:
    return False


def _prepared_cache(root: Path) -> Path:
    cache = root / "hf" / "datasets"
    data = cache / CACHED / "default" / "0.0.0" / "abc"
    data.mkdir(parents=True)
    (data / "train.arrow").write_bytes(b"arrow")
    (data / "dataset_info.json").write_text("{}")
    return cache


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        ({"HF_DATASETS_CACHE": "/d", "HF_HOME": "/h", "XDG_CACHE_HOME": "/x"}, "/d"),
        ({"HF_HOME": "/h", "XDG_CACHE_HOME": "/x"}, "/h/datasets"),
        ({"XDG_CACHE_HOME": "/x"}, "/x/huggingface/datasets"),
    ],
)
def test_the_datasets_cache_resolves_as_the_datasets_library_does(
    environ: dict[str, str], expected: str
) -> None:
    assert datasets_cache(environ) == Path(expected)


def test_a_writable_datasets_cache_is_used_as_it_is(tmp_path: Path) -> None:
    cache = _prepared_cache(tmp_path)
    environ = {"HF_HOME": str(cache.parent), "OTHER": "kept"}
    with harness_environment(environ, tmp_path / "scratch", writable=lambda _: True) as env:
        assert env == environ
    assert not (tmp_path / "scratch").exists()


def test_a_read_only_datasets_cache_is_read_through_a_writable_copy(tmp_path: Path) -> None:
    cache = _prepared_cache(tmp_path)
    environ = {"HF_HOME": str(cache.parent), "OTHER": "kept"}
    scratch = tmp_path / "scratch"
    with harness_environment(environ, scratch, writable=_read_only) as env:
        shadow = Path(env["HF_DATASETS_CACHE"])
        assert shadow.is_relative_to(scratch)
        assert {k: v for k, v in env.items() if k != "HF_DATASETS_CACHE"} == environ
        copied = shadow / CACHED / "default" / "0.0.0" / "abc" / "train.arrow"
        assert copied.read_bytes() == b"arrow"
        (shadow / "a.lock").write_text("")  # the cache root takes the library's lock files
    assert not shadow.exists()
    assert sorted(p.name for p in cache.iterdir()) == [CACHED]


def test_the_copy_is_removed_when_the_replay_fails(tmp_path: Path) -> None:
    cache = _prepared_cache(tmp_path)
    scratch = tmp_path / "scratch"
    failure = HarnessError("aiperf exited 1")
    with (
        pytest.raises(HarnessError),
        harness_environment({"HF_DATASETS_CACHE": str(cache)}, scratch, writable=_read_only),
    ):
        raise failure
    assert not (scratch / "hf-datasets").exists()


def test_a_read_only_cache_without_the_corpus_is_refused_naming_it(tmp_path: Path) -> None:
    cache = tmp_path / "empty"
    cache.mkdir()
    with (
        pytest.raises(AgentxError, match=rf"{cache}.*read-only.*{HF_DATASET}"),
        harness_environment({"HF_DATASETS_CACHE": str(cache)}, tmp_path, writable=_read_only),
    ):
        pass
