"""Tests for the reusable dial9 trace ranker's policy path.

``skills/domains/dial9-tokio/ranker/`` is first-party tooling shipped inside
the dial9-tokio skill bundle: it ranks any Tokio runtime's dial9 trace into
profile result protocol v1, independent of any example. The heavy parts
(building and running an engine, parsing a real trace with the Node toolkit) are
not exercised here. Instead a captured, trimmed extractor report
(``fixtures/dial9_extractor_report.json``) is fed through the pure policy path
(``analyze_dial9.validate`` / ``build_result``).

The ranker is loaded by file path because it ships as a standalone script, not
an importable package. Its output is round-tripped through the real
profile-guided protocol validator, so this proves the ranker satisfies the
contract the orchestration loop reads, not merely a hand-checked shape.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.profile_focus.attribution import parse_attribution

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import ModuleType

# The framing the runtime wraps a profiler's result in before `parse_attribution`
# reads it. Pinned here as the published contract rather than imported from the
# module's private names, so a change to the markers breaks this test loudly.
_FRAME_BEGIN = "__VIBESYS_ATTRIBUTION_BEGIN__"
_FRAME_END = "__VIBESYS_ATTRIBUTION_END__"

_REPO = Path(__file__).resolve().parents[2]
_ANALYZE_PY = _REPO / "skills" / "domains" / "dial9-tokio" / "ranker" / "analyze_dial9.py"
_FIXTURE = Path(__file__).with_name("fixtures") / "dial9_extractor_report.json"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, str(path))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Registering before exec is required: the module uses `from __future__
    # import annotations` with a frozen dataclass / typed helpers, and the
    # dataclass machinery resolves the module out of sys.modules at class-build
    # time.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def analyze() -> ModuleType:
    return _load_module("dial9_analyze_under_test", _ANALYZE_PY)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text())


def test_fixture_builds_protocol_v1(analyze: ModuleType, report: dict[str, Any]) -> None:
    analyze.validate(report)
    result = analyze.build_result(report)

    assert result["version"] == 1
    assert result["cost_unit"]
    names = [c["name"] for c in result["components"]]
    assert len(names) == len(set(names)), "component names must be unique"
    assert sum(c["share"] for c in result["components"]) <= 1 + 1e-9
    for component in result["components"]:
        assert component["cost"] >= 0
        assert 0 <= component["share"] <= 1
        assert component["evidence"]
        assert all(isinstance(line, str) for line in component["evidence"])


def test_evidence_distinguishes_slow_from_starved(
    analyze: ModuleType, report: dict[str, Any]
) -> None:
    result = analyze.build_result(report)
    by_name = {c["name"]: c for c in result["components"]}

    starved = by_name["fragment 4 StreamProject"]
    assert any("off-CPU" in line for line in starved["evidence"]), (
        "a fragment whose wall clock dwarfs its on-CPU time must read as starved"
    )
    wrapper = by_name["fragment 3 StreamNoOp"]
    assert any("nested spans" in line for line in wrapper["evidence"]), (
        "a span with instrumented children must show cumulative-vs-self time"
    )
    for component in result["components"]:
        assert component["evidence"][-1].startswith("run:"), "run context on every component"


def test_build_result_preserves_order_and_is_deterministic(
    analyze: ModuleType, report: dict[str, Any]
) -> None:
    first = analyze.build_result(report)
    second = analyze.build_result(report)
    assert first == second
    assert [c["name"] for c in first["components"]] == [c["name"] for c in report["components"]]


def test_result_parses_against_the_protocol(analyze: ModuleType, report: dict[str, Any]) -> None:
    result = analyze.build_result(report)
    framed = f"prefix noise\n{_FRAME_BEGIN}\n{json.dumps(result)}\n{_FRAME_END}\ntrailing noise"

    bottlenecks = parse_attribution(framed)

    assert len(bottlenecks) == len(report["components"])
    costs = [b.cost for b in bottlenecks]
    assert costs == sorted(costs, reverse=True), "protocol ranks by descending cost"
    assert bottlenecks[0].name == "fragment 2 StreamHashJoin"
    for bottleneck in bottlenecks:
        assert 0 <= bottleneck.share <= 1


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda r: r.update({"schema": 2}), id="schema-bump"),
        pytest.param(lambda r: r["trace"].update({"hasFullTaskCoverage": False}), id="no-coverage"),
        pytest.param(lambda r: r["trace"].update({"hasSpans": False}), id="no-spans"),
        pytest.param(lambda r: r["totals"].update({"pollOnCpuNs": 0}), id="no-poll-time"),
        pytest.param(
            lambda r: r["totals"].update({"unresolvedSpanCount": 1}), id="unresolved-span"
        ),
        pytest.param(lambda r: r.update({"components": []}), id="no-components"),
        pytest.param(
            lambda r: r["diagnostics"].append({"check": "missing-frame-pointers", "message": "x"}),
            id="fatal-diagnostic",
        ),
        pytest.param(
            lambda r: r["diagnostics"].append({"check": "totally-made-up", "message": "x"}),
            id="unknown-diagnostic-fails-closed",
        ),
    ],
)
def test_validate_rejects_untrustworthy_report(
    analyze: ModuleType, report: dict[str, Any], mutate: Callable[[dict[str, Any]], None]
) -> None:
    broken = copy.deepcopy(report)
    mutate(broken)
    with pytest.raises(analyze.SetupError):
        analyze.validate(broken)


def test_validate_tolerates_cosmetic_diagnostic(
    analyze: ModuleType, report: dict[str, Any]
) -> None:
    tolerable = copy.deepcopy(report)
    tolerable["diagnostics"].append({"check": "missing-debug-symbols", "message": "x"})
    analyze.validate(tolerable)  # must not raise: it only costs readable frame names


@st.composite
def _reports(draw: st.DrawFn) -> dict[str, Any]:
    count = draw(st.integers(min_value=1, max_value=6))
    names = draw(
        st.lists(st.text(min_size=1, max_size=12), min_size=count, max_size=count, unique=True)
    )
    components = []
    for name in names:
        self_ns = draw(st.integers(min_value=0, max_value=10**12))
        components.append(
            {
                "name": name,
                "selfOnCpuNs": self_ns,
                "onCpuNs": self_ns,
                "wallNs": self_ns,
                "share": draw(st.floats(min_value=0, max_value=1, allow_nan=False)),
                "count": draw(st.integers(min_value=1, max_value=64)),
                "taskCount": draw(st.integers(min_value=1, max_value=64)),
            }
        )
    return {
        "components": components,
        "totals": {"pollOnCpuNs": 10**12, "unattributedOnCpuNs": 0},
        "workers": [{"utilization": 0.3}],
        "schedDelay": {"count": 1, "p50Ns": 1, "p99Ns": 2},
        "queueDepth": {"max": 1, "avg": 0.1, "samples": 1},
    }


@given(report=_reports())
def test_build_result_invariants_hold_for_any_report(
    analyze: ModuleType, report: dict[str, Any]
) -> None:
    result = analyze.build_result(report)
    assert result["version"] == 1
    assert [c["name"] for c in result["components"]] == [c["name"] for c in report["components"]]
    assert result == analyze.build_result(report)
    for built, source in zip(result["components"], report["components"], strict=True):
        assert built["cost"] == pytest.approx(source["selfOnCpuNs"] / 1e9)
        assert built["cost"] >= 0
        assert 0 <= built["share"] <= 1
        assert built["evidence"]
        assert all(isinstance(line, str) for line in built["evidence"])
