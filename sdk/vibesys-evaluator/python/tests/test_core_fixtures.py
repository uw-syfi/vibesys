"""``fixtures/core.json``: what the Python wrappers and the Go core must both come out with.

The cases were written from the Python implementation the core replaced; the Go package's tests
(``go/evaluator``) check the same file. Here the wrappers run the core: HDR histograms and
merges, result-protocol-2 records byte for byte, sample seeds and trial aggregation go through
the binary, and ``Metric`` and ``Validity`` (native, plain data) must refuse what Go refuses with
the same words.
"""

from __future__ import annotations

import base64
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from vibesys_evaluator import Metric, Validity
from vibesys_evaluator._private import core
from vibesys_evaluator._private.hdr import HistogramError, decode, summarize
from vibesys_evaluator._private.records import Partial, RecordError, RecordStream
from vibesys_evaluator._private.sample import Mode, _new_sample

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURE = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "core.json").read_text())


def _number(value: float | str) -> float:
    return {"NaN": math.nan, "Infinity": math.inf, "-Infinity": -math.inf}.get(value, value)


@pytest.mark.parametrize("case", FIXTURE["histograms"], ids=lambda c: c["name"])
def test_a_histogram_decodes_to_the_recorded_summary(case: dict[str, Any]) -> None:
    histogram = decode(base64.b64decode(case["encoded"]))
    assert (histogram.total, histogram.max) == (case["count"], case["max"])
    count, maximum, values = summarize([base64.b64decode(case["encoded"])], FIXTURE["percentiles"])
    assert (count, maximum, values) == (case["count"], case["max"], case["percentiles"])
    assert histogram.percentile(99) == case["percentiles"][FIXTURE["percentiles"].index(99)]


@pytest.mark.parametrize("case", FIXTURE["merges"], ids=lambda c: c["name"])
def test_merged_histograms_have_the_recorded_summary(case: dict[str, Any]) -> None:
    encoded = [base64.b64decode(e) for e in case["encoded"]]
    assert summarize(encoded, FIXTURE["percentiles"]) == (
        case["count"],
        case["max"],
        case["percentiles"],
    )


@pytest.mark.parametrize(
    "data",
    [b"", b"\x00\x00\x00\x00\x00\x00\x00\x00", b"HISTF!!!", b"\x1c\x84\x93\x04\x00\x00\x00\x02xx"],
    ids=["empty", "bad-cookie", "bad-base64", "bad-zlib"],
)
def test_garbage_is_refused(data: bytes) -> None:
    with pytest.raises(HistogramError):
        decode(data)


@pytest.mark.parametrize("case", FIXTURE["records"], ids=lambda c: c["name"])
def test_a_record_stream_is_byte_for_byte_what_python_wrote(
    case: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "out.jsonl"
    stream = RecordStream(path)
    errors: list[str | None] = []
    for op in case["ops"]:
        try:
            if op["op"] == "hello":
                stream.hello(
                    {
                        m["name"]: Metric(unit=m["unit"], direction=m["direction"])
                        for m in op["metrics"]
                    }
                )
            elif op["op"] == "result":
                stream.result({name: _number(value) for name, value in op["values"]})
            else:
                partial = op["partial"]
                stream.error(
                    op["message"],
                    partial=None
                    if partial is None
                    else Partial(
                        name=partial["name"],
                        value=_number(partial["value"]),
                        direction=partial["direction"],
                        unit=partial.get("unit", ""),
                    ),
                )
            errors.append(None)
        except RecordError as error:
            errors.append(str(error))
    stream.close()
    assert errors == case["expect"]["errors"]
    assert (path.read_text() if path.exists() else "") == case["expect"]["file"]


def _refusal(build: Callable[[], object]) -> str | None:
    try:
        build()
    except (ValueError, TypeError) as error:
        return str(error)
    return None


@pytest.mark.parametrize("case", FIXTURE["validity"])
def test_validity_refuses_what_it_always_refused(case: dict[str, Any]) -> None:
    refused = _refusal(
        lambda: Validity(
            max_error_rate=_number(case["max_error_rate"]),
            min_sustained_ratio=_number(case["min_sustained_ratio"]),
            min_ops_per_operation=case["min_ops_per_operation"],
        )
    )
    assert refused == case["error"]


@pytest.mark.parametrize("case", FIXTURE["metrics"])
def test_metric_refuses_what_it_always_refused(case: dict[str, Any]) -> None:
    assert _refusal(lambda: Metric(unit="u", direction=case["direction"])) == case["error"]


@pytest.mark.parametrize("case", FIXTURE["seeds"], ids=lambda c: f"{c['flag']}-{c['env']!r}")
def test_the_sample_seed_comes_from_the_flag_the_environment_or_a_draw(
    case: dict[str, Any], tmp_path: Path
) -> None:
    env = {"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "artifacts")}
    if case["env"]:
        env["VIBESYS_SAMPLE_SEED"] = case["env"]
    flag = None if case["flag"] is None else int(case["flag"])
    expect = case["expect"]
    if "error" in expect:
        with pytest.raises(ValueError, match=r".+") as raised:
            _new_sample(Mode.CHECK, seed_flag=flag, env=env, scratch=tmp_path)
        assert str(raised.value) == expect["error"]
        return
    sample = _new_sample(Mode.CHECK, seed_flag=flag, env=env, scratch=tmp_path)
    seeds = json.loads((tmp_path / "artifacts" / "seeds.json").read_text())
    assert seeds == {"sample_seed": sample.seed, "source": expect["source"]}
    if expect["source"] != "drawn":
        assert sample.seed == int(expect["seed"])


_AGGREGATIONS = {
    "median": "AGGREGATION_MEDIAN",
    "mean": "AGGREGATION_MEAN",
    "min": "AGGREGATION_MIN",
    "max": "AGGREGATION_MAX",
    "sum": "AGGREGATION_SUM",
}


def test_trials_combine_into_the_value_python_statistics_gives() -> None:
    series = [
        {"name": f"s{n}", "aggregation": _AGGREGATIONS[c["aggregation"]], "values": c["trials"]}
        for n, c in enumerate(FIXTURE["combine"])
    ]
    reply = core.call("sample", "combine", {"series": series})
    assert [v["value"] for v in reply["values"]] == [c["want"] for c in FIXTURE["combine"]]
